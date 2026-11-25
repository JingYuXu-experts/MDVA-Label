"""Project-owned solar images, immutable batch selections and inference queue views."""

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import base64
import hashlib
import io
import json
import re
import subprocess
import time
from functools import lru_cache
from threading import Lock
import uuid
import zipfile

from PIL import Image, ImageOps

from . import solar_models as models
from .solar_input import MAX_BYTES, decode_image, input_identity
from ..training.durable import atomic_json, commit
from ..training.runtime import digest, now
from ..training.solar_device import CPU
from ..training.solar_forecast import HMI_IMAGE_SIZE
from ..training.solar_schedule import PENDING, Timing, job_device, targets

_probe_lock = Lock()

CHANNELS = {"AR": "AR-A0-s42", "CH": "CH-C2-s2026"}
TARGET_CHANNELS = {"flare-risk": "AR", "coronal-hole": "CH"}
INSTRUMENT_CHANNELS = {"HMI": "AR", "AIA193": "CH"}
CHANNEL_LABELS = {"AR": "HMI 磁图", "CH": "AIA 193 Å 观测图"}


def project_home(hub):
    return hub.store.require_writable(Path(hub.out_dir) / "solar-projects" / models.workspace_key(hub))


def project_view(dataset, preferences):
    return {
        "id": dataset["id"],
        "name": dataset["name"],
        "managed": dataset["meta"].get("category") == "solar-project",
        "pinned": preferences.get("pinned", False),
        "opened_at": preferences.get("opened_at"),
        "opens": preferences.get("opens", 0),
    }


def projects(hub):
    preferences = models.read(project_home(hub) / "preferences.json", {})
    return [
        project_view(d, preferences.get(d["id"], {}))
        for d in hub.store.list_datasets()
        if d["type"] == "segmentation"
    ]


def update_project(hub, project, action, pinned=False):
    dataset, _ = location(hub, project)
    path = project_home(hub) / "preferences.json"
    with models._lock:
        preferences = models.read(path, {})
        entry = preferences.setdefault(project, {})
        if action == "open":
            entry["opened_at"] = now()
            entry["opens"] = entry.get("opens", 0) + 1
        elif action == "pin":
            entry["pinned"] = pinned
        else:
            raise ValueError("未知项目操作")
        atomic_json(path, preferences)
    return project_view(dataset, entry)


def create(hub, name):
    name = name.strip()
    if not 1 <= len(name) <= 120:
        raise ValueError("项目名称需为 1 至 120 个字符")
    root = hub.store.require_writable(Path(hub.out_dir) / "solar-project-images" / uuid.uuid4().hex)
    root.mkdir(parents=True)
    with hub.store.transaction():
        created = hub.create_dataset(
            name, "segmentation", root, category="solar-project", labels=["AR", "CH", "FL"]
        )
    return project_view(hub.store.get_dataset(created["dataset_id"]), {})


def location(hub, project):
    ds = hub.store.get_dataset(project)
    if not ds or ds["type"] != "segmentation":
        raise ValueError("请选择当前工作区的图像项目")
    folder = hub.store.require_writable(project_home(hub) / ds["id"])
    return ds, folder


def catalog(hub, project):
    _, folder = location(hub, project)
    photos = models.read(folder / "photos.json", {})
    batches = [models.read(p) for p in (folder / "batches").glob("*.json")]
    latest = {}
    for batch in sorted(batches, key=lambda b: b["created"]):
        for item in batch["selection"]:
            latest[item["id"]] = batch["id"]
    return {
        "photos": [public_photo(p) | {"last_batch": latest.get(p["id"])} for p in photos.values()],
        "batches": [
            {"id": b["id"], "created": b["created"], "total": len(b["selection"])}
            for b in sorted(batches, key=lambda b: b["created"], reverse=True)
        ],
    }


def public_photo(photo):
    return {k: v for k, v in photo.items() if k not in ("uri", "pixel_hashes")}


@lru_cache(maxsize=12)
def read_source_index(path, stamp):
    return models.read(path, {})


def source_index(hub, project, *, refresh=False):
    dataset, folder = location(hub, project)
    path = folder / "source-images.json"
    version = hub.head_version(project)
    if not refresh and path.is_file():
        snapshot = read_source_index(path, path.stat().st_mtime_ns)
        if snapshot.get("version") == version:
            return snapshot["members"]
    if dataset["meta"].get("indexed"):
        uris = (row["uri"] for row in hub.store.version_records(version))
    else:
        uris = hub._disk_uris(dataset, "image")
    members = {
        hashlib.sha256(uri.encode()).hexdigest(): uri
        for uri in uris
        if Path(uri).suffix.lower() in (".jpg", ".jpeg", ".png")
    }
    atomic_json(path, {"version": version, "members": members})
    return members


def existing_images(hub, project, search, page, *, refresh=False, ids_only=False):
    members = source_index(hub, project, refresh=refresh)
    query = search.strip().casefold()
    matches = [
        (key, Path(uri).name) for key, uri in members.items() if query in Path(uri).name.casefold()
    ]
    if ids_only:
        return {"ids": [key for key, _ in matches]}
    start = max(0, page - 1) * 60
    return {
        "images": [{"id": key, "name": name} for key, name in matches[start : start + 60]],
        "total": len(matches),
    }


def source_path(hub, project, identifier):
    _, folder = location(hub, project)
    index = folder / "source-images.json"
    snapshot = read_source_index(index, index.stat().st_mtime_ns) if index.is_file() else {}
    uri = snapshot.get("members", {}).get(identifier)
    if not uri or snapshot["version"] != hub.head_version(project):
        raise ValueError("项目照片列表已变化，请重新打开列表")
    return hub.store.require_within(uri)


def source_thumbnail(hub, project, identifier):
    source = source_path(hub, project, identifier)
    if source.stat().st_size > MAX_BYTES:
        raise ValueError("图像超过 4 MiB")
    try:
        with Image.open(source) as image:
            if image.width * image.height > 32_000_000:
                raise ValueError("图像尺寸过大，无法预览")
            image.draft("RGB", (240, 240))
            preview = ImageOps.exif_transpose(image)
            preview.thumbnail((240, 240))
            stream = io.BytesIO()
            preview.convert("RGB").save(stream, "JPEG", quality=82)
            return stream.getvalue()
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("无法预览此照片") from exc


def import_sources(hub, project, identifiers):
    if not 1 <= len(identifiers) <= 40 or any(not isinstance(key, str) for key in identifiers):
        raise ValueError("每次请求可载入 1 至 40 张已有照片")
    members = source_index(hub, project)
    results = []
    for identifier in dict.fromkeys(identifiers):
        row = {"id": identifier, "name": Path(members.get(identifier, "照片")).name}
        try:
            row.update(import_photo(hub, project, {"source_id": identifier}))
        except ValueError as exc:
            row["error"] = str(exc)
        except OSError:
            row["error"] = "无法读取或保存这张照片"
        results.append(row)
    return {"results": results}


def import_photo(hub, project, params):
    ds, folder = location(hub, project)
    if params.get("source_id"):
        path = source_path(hub, project, params["source_id"])
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("图像不得超过4MiB")
        mime = "png" if path.suffix.lower() == ".png" else "jpeg"
        params = {
            "filename": path.name,
            "image_data": "data:image/"
            + mime
            + ";base64,"
            + base64.b64encode(path.read_bytes()).decode(),
        }
    if params.get("catalog_uri"):
        from .solar_catalog import SolarCatalog

        uri = str(params["catalog_uri"])
        raw = SolarCatalog(hub).image(uri)
        if len(raw) > MAX_BYTES:
            raise ValueError("图像不得超过4MiB")
        mime = "png" if Path(uri).suffix.lower() == ".png" else "jpeg"
        params = {
            "filename": Path(uri).name,
            "image_data": f"data:image/{mime};base64," + base64.b64encode(raw).decode(),
        }
    name, raw, width, height, pixels, suffix = decode_image(params)
    identifier = hashlib.sha256(raw).hexdigest()
    try:
        known = input_identity(models.experiment(hub.out_dir), identifier, pixels)
    except FileNotFoundError:
        # The library is usable before a model and its frozen manifests are installed.
        known = {}
    channel = INSTRUMENT_CHANNELS.get(known.get("instrument"), "unsupported" if known else "")
    with models._lock:
        index = models.read(folder / "photos.json", {})
        if identifier in index:
            return {"photo": public_photo(index[identifier]), "duplicate": True}
        root = Path(ds["root"]) if ds["meta"].get("category") == "solar-project" else folder / "images"
        path = hub.store.require_writable(root / (identifier + suffix))
        commit(path, lambda f: f.write(raw))
        with Image.open(io.BytesIO(raw)) as im:
            im.thumbnail((192, 192))
            commit(
                folder / "thumbs" / (identifier + ".jpg"), lambda f: im.convert("RGB").save(f, "JPEG")
            )
        photo = {
            "id": identifier,
            "pixel_hashes": sorted(pixels),
            "name": name,
            "width": width,
            "height": height,
            "category": channel,
            "observed_utc": known.get("time_utc"),
            "instrument": known.get("instrument"),
            "reference_uri": known.get("uri"),
            "uri": hub.store.to_rel(path),
        }
        index[identifier] = photo
        atomic_json(folder / "photos.json", index)
        return {"photo": public_photo(photo), "duplicate": False}


def photo_asset(hub, project, identifier, thumb):
    _, folder = location(hub, project)
    photo = models.read(folder / "photos.json", {}).get(identifier)
    if not photo:
        raise ValueError("照片不属于当前项目")
    if thumb:
        return (folder / "thumbs" / (identifier + ".jpg")).read_bytes(), "image/jpeg"
    path = hub.store.require_within(photo["uri"])
    return path.read_bytes(), "image/png" if path.suffix == ".png" else "image/jpeg"


@lru_cache(maxsize=2)
def _devices(binary, minute):
    # Probe the model interpreter without importing Torch into the HTTP server.
    try:
        run = subprocess.run(
            [str(binary), "-m", "mdva.training.solar_device"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if run.returncode == 0:
            return json.loads(run.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return [CPU]


def hardware(hub):
    binary, _ = models.model_python()
    with _probe_lock:
        return _devices(binary, int(time.monotonic() // 30))


def queue_timing(hub, devices):
    jobs = (models.read(path) for path in models.job_root(hub.out_dir).glob("*/job.json"))
    return Timing(list(jobs), devices, time.time())


def gpu_blocked(hub):
    from ..training.continuation import lock_alive
    from ..training.solar_worker import final_test_running

    lock = models.ROOT / ".mdva/training-gpu.lock"
    state = lock_alive(lock)
    try:
        owner = models.read(lock, {}) if state is True else {}
        worker = models.read(models.job_root(hub.out_dir) / "worker.lock", {})
    except (ValueError, OSError):
        return True
    if owner and owner.get("pid") == worker.get("pid"):
        return False
    return state is None or bool(owner) or final_test_running(hub.out_dir)


def estimate(hub, categories, mode="auto"):
    devices = hardware(hub)
    timing = queue_timing(hub, devices)
    counts = {CHANNELS[category]: categories[category] for category in sorted(categories)}
    plans = defaultdict(list)
    if counts:
        for model, shards in timing.plan(counts, targets(mode, devices)).items():
            for shard in shards:
                for offset in range(0, shard["count"], 200):
                    plans[shard["device"]["id"]].append(
                        {
                            "model_id": model,
                            "total": min(200, shard["count"] - offset),
                        }
                    )
    estimate = timing.estimate(
        [(device, plans[device["id"]]) for device in devices if device["id"] in plans],
        gpu_blocked(hub) if any(key.startswith("cuda:") for key in plans) else False,
    )
    return {**estimate, "hardware": devices}


def batch_estimate(hub, jobs):
    if not jobs:
        return None
    devices = hardware(hub)
    timing = queue_timing(hub, devices)
    pending = {job["id"] for job in jobs}
    plans = defaultdict(list)
    for job in timing.work:
        if job["id"] not in pending:
            continue
        device = job_device(job, devices)
        if device is None:
            continue
        plans[device["id"]].append(job)
    return (
        timing.estimate(
            [(device, plans[device["id"]]) for device in devices if device["id"] in plans],
            gpu_blocked(hub) if any(key.startswith("cuda:") for key in plans) else False,
        )
        if plans
        else None
    )


def _batch_file(folder, identifier):
    if not re.fullmatch("[a-f0-9]{32}", identifier):
        raise ValueError("批次编号不合法")
    return folder / "batches" / (identifier + ".json")


def utc_value(value):
    if value is None or value == "":
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not stamp.tzinfo:
            raise ValueError
        return stamp.astimezone(timezone.utc)
    except (ValueError, AttributeError, OverflowError) as exc:
        raise ValueError("观测时间须为有效的带时区时间") from exc


def observation_window(window):
    window = window or {}
    start, end = (utc_value(window.get(key)) for key in ("start_utc", "end_utc"))
    if start is not None and end is not None and start > end:
        raise ValueError("观测起始时间不能晚于结束时间")
    return start, end


def selected_photos(folder, selection):
    if not isinstance(selection, list) or not selection:
        raise ValueError("请先选择照片")
    photos = models.read(folder / "photos.json", {})
    seen = set()
    for row in selection:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise ValueError("照片选择格式无效")
        key = row["id"]
        if key in seen or key not in photos:
            raise ValueError("照片重复或不属于当前项目")
        seen.add(key)
        yield row, photos[key]


def photo_identity(exp, photo):
    try:
        return input_identity(exp, photo["id"], set(photo.get("pixel_hashes", ())))
    except FileNotFoundError as exc:
        raise ValueError("缺少识别模型的观测清单，请检查模型安装") from exc


def photo_source(hub, photo, *, check_format=False):
    source = hub.store.require_within(photo["uri"])
    try:
        if check_format and (
            source.suffix.lower() not in (".jpg", ".jpeg", ".png") or source.stat().st_size > MAX_BYTES
        ):
            raise ValueError("只支持不超过 4 MiB 的 JPG、JPEG 或 PNG 显示图")
        if digest(source) != photo["id"]:
            raise ValueError("已导入的照片发生变化，请重新导入")
    except OSError as exc:
        raise ValueError("无法读取已导入的照片，请检查文件是否存在") from exc
    return source


def task_input(row, photo, observed, source, catalog_time):
    return {
        "photo_id": row["id"],
        "path": str(source),
        "uri": photo["uri"],
        "sha256": row["id"],
        "observed_utc": observed.isoformat() if observed else None,
        "time_basis": "catalog" if catalog_time else "user_supplied",
    }


def channel_info(photo, identity, manual_channel):
    instrument = str(identity.get("instrument") or photo.get("instrument") or "").strip().upper()
    documented = INSTRUMENT_CHANNELS.get(instrument, "unsupported" if instrument else photo["category"])
    category = documented or (manual_channel if manual_channel in ("AR", "CH") else "")
    if instrument == "HMI" or (not instrument and category == "AR"):
        family = "hmi"
    elif instrument.startswith("AIA") or (not instrument and category == "CH"):
        family = "aia"
    else:
        family = "other" if instrument or documented == "unsupported" else "unknown"
    return {
        "instrument": instrument,
        "category": category,
        "data_type": family,
        "can_confirm_channel": not documented,
    }


def screen_photo(hub, row, photo, identity, wanted, window, available):
    channel = channel_info(photo, identity, row.get("category"))
    instrument, category = channel["instrument"], channel["category"]
    catalog_time = identity.get("time_utc") or photo["observed_utc"]
    start, end = window
    requires_time = wanted == "AR" or start is not None or end is not None
    public = {
        "id": photo["id"],
        "name": photo["name"],
        **channel,
        "can_supply_time": not catalog_time and requires_time and category in ("", wanted),
    }

    def excluded(status, reason):
        return {**public, "status": status, "reason": reason}, None

    if category == "unsupported":
        return excluded("unsupported", f"当前模型未适配 {instrument or '该观测数据'}")
    try:
        source = photo_source(hub, photo, check_format=True)
    except ValueError as exc:
        public.update(can_confirm_channel=False, can_supply_time=False)
        return excluded("unsupported", str(exc))
    if category not in CHANNELS:
        return excluded("pending", "缺少可核对的观测通道，请按原始观测来源确认")
    if category != wanted:
        return excluded(
            "mismatch", f"此图为 {CHANNEL_LABELS[category]}，当前目标需要 {CHANNEL_LABELS[wanted]}"
        )
    if wanted == "AR" and (photo.get("width"), photo.get("height")) != HMI_IMAGE_SIZE:
        return excluded("unsupported", "耀斑风险试算要求原生 1024 × 1024 完整日面 HMI 磁图")
    model = available.get(CHANNELS[wanted])
    if not model or not model["available"]:
        return excluded("unsupported", (model or {}).get("reason") or "尚未安装当前目标的识别模型")
    try:
        observed = utc_value(catalog_time or row.get("observed_utc"))
    except ValueError as exc:
        return excluded("pending", str(exc))
    if requires_time and observed is None:
        return excluded(
            "pending", "匹配 GOES 记录需要观测时间" if wanted == "AR" else "按时段筛选需要观测时间"
        )
    if wanted == "AR" and observed > datetime.now(timezone.utc):
        return excluded("pending", "观测时间晚于当前时刻，请核对来源或输入")
    if (start is not None and observed < start) or (end is not None and observed > end):
        return excluded("outside_time", f"观测时刻 {observed.isoformat()} 不在选定的 UTC 时间范围内")
    return {**public, "status": "eligible", "reason": ""}, task_input(
        row, photo, observed, source, catalog_time
    )


def screen_selection(hub, folder, selection, target, window, exp, available):
    wanted = TARGET_CHANNELS.get(target)
    if not wanted:
        raise ValueError("请先选择识别目标")
    checked = []
    inputs = []
    for row, photo in selected_photos(folder, selection):
        try:
            identity = photo_identity(exp, photo) if exp is not None else {}
        except ValueError as exc:
            checked.append(
                {
                    "id": photo["id"],
                    "name": photo["name"],
                    "status": "unsupported",
                    "reason": str(exc),
                    **channel_info(photo, {}, row.get("category")),
                    "can_confirm_channel": False,
                    "can_supply_time": False,
                }
            )
            continue
        result, item = screen_photo(hub, row, photo, identity, wanted, window, available)
        checked.append(result)
        if item is not None:
            inputs.append(item)
    return checked, inputs


def precheck(hub, project, selection, target, window=None):
    _, folder = location(hub, project)
    bounds = observation_window(window)
    available = {model["id"]: model for model in models.inventory(hub.out_dir)["models"]}
    try:
        exp = models.experiment(hub.out_dir)
    except FileNotFoundError:
        exp = None
    checked, inputs = screen_selection(hub, folder, selection, target, bounds, exp, available)
    accepted = {item["photo_id"]: item for item in inputs}
    return {
        "target": target,
        "photos": checked,
        "selection": [
            {
                "id": row["id"],
                "category": row["category"],
                "observed_utc": accepted[row["id"]]["observed_utc"],
            }
            for row in checked
            if row["id"] in accepted
        ],
    }


def submit(hub, project, identifier, selection, mode="auto", *, target="", window=None):
    _, folder = location(hub, project)
    path = _batch_file(folder, identifier)
    if not isinstance(selection, list) or not selection:
        raise ValueError("没有符合当前目标的照片可以入队" if target else "请先选择照片")
    window = window or {}
    if window and not target:
        raise ValueError("使用观测时间筛选时请选择识别目标")
    bounds = observation_window(window)
    with models._lock:
        batch = models.read(path)
        if batch and (
            batch["selection"] != selection
            or batch.get("mode", "auto") != mode
            or batch.get("target", "") != target
            or batch.get("window", {}) != window
        ):
            raise ValueError("该确认请求的选区或处理设备已变化")
        if batch and batch.get("jobs"):
            return {"id": identifier}
        available = {m["id"]: m for m in models.inventory(hub.out_dir)["models"]}
        if not available:
            raise ValueError("尚未安装太阳识别模型，已导入的照片可继续浏览")
        exp = models.experiment(hub.out_dir)
        groups = defaultdict(list)
        if target:
            reviewed, inputs = screen_selection(hub, folder, selection, target, bounds, exp, available)
            rejected = next((row for row in reviewed if row["status"] != "eligible"), None)
            if rejected:
                raise ValueError(
                    f"入队校验未通过：{rejected['name']} · {rejected['reason']}。请重新检查。"
                )
            groups[TARGET_CHANNELS[target]] = inputs
        else:
            for row, photo in selected_photos(folder, selection):
                identity = photo_identity(exp, photo)
                catalog_channel = INSTRUMENT_CHANNELS.get(
                    identity.get("instrument"), "unsupported" if identity else ""
                )
                category = catalog_channel or photo["category"] or row.get("category")
                if category != row.get("category") or category not in CHANNELS:
                    raise ValueError("请确认照片的观测通道")
                model = available.get(CHANNELS[category])
                if not model or not model["available"]:
                    raise ValueError((model or {}).get("reason") or "模型不可用")
                source = photo_source(hub, photo)
                catalog_time = identity.get("time_utc") or photo["observed_utc"]
                observed = utc_value(catalog_time or row.get("observed_utc"))
                groups[category].append(task_input(row, photo, observed, source, catalog_time))
        if not batch:
            plan = None
            if mode != "auto":
                devices = hardware(hub)
                selected = targets(mode, devices)
                plan = queue_timing(hub, devices).plan(
                    {CHANNELS[category]: len(groups[category]) for category in sorted(groups)},
                    selected,
                )
            batch = {
                "id": identifier,
                "created": now(),
                "selection": selection,
                "version": hub.head_version(project),
                "mode": mode,
                "target": target,
                "window": window,
                "plan": plan,
                "jobs": [],
            }
            atomic_json(path, batch)
        children = []
        for category, items in sorted(groups.items()):
            model_id = CHANNELS[category]
            shards = (batch.get("plan") or {}).get(model_id, [{"device": None, "count": len(items)}])
            offset = 0
            for shard in shards:
                limit = offset + shard["count"]
                for start in range(offset, limit, 200):
                    chunk = items[start : min(start + 200, limit)]
                    job = models.enqueue(
                        hub,
                        hub.out_dir,
                        available[model_id],
                        exp,
                        chunk,
                        project,
                        batch["version"],
                        f"{project}_{identifier}_{category}_{start}",
                        spawn=False,
                        target_device=shard["device"],
                    )
                    children.append({"id": job["id"], "photos": [i["photo_id"] for i in chunk]})
                offset = limit
        batch["jobs"] = children
        atomic_json(path, batch)
        models.ensure_worker(hub.out_dir)
    return {"id": identifier}


def status(hub, project, identifier):
    _, folder = location(hub, project)
    batch = models.read(_batch_file(folder, identifier))
    if not batch:
        raise ValueError("此项目没有该批次")
    if not batch["jobs"]:
        submit(
            hub,
            project,
            identifier,
            batch["selection"],
            batch.get("mode", "auto"),
            target=batch.get("target", ""),
            window=batch.get("window"),
        )
        batch = models.read(_batch_file(folder, identifier))
    models.ensure_worker(hub.out_dir)
    rows = {}
    jobs = []
    for child in batch["jobs"]:
        directory, job = models._job(hub.out_dir, child["id"], hub)
        results = {r["index"]: r for r in job["results"]}
        if job["state"] in PENDING:
            jobs.append(job)
        for index, photo in enumerate(child["photos"]):
            result = results.get(index, {})
            state = result.get("state") or (
                "running"
                if job["state"] == "running" and job.get("active_index") == index
                else (
                    "failed"
                    if job["state"] == "failed"
                    else "waiting_gpu" if job["state"] == "waiting_gpu" else "queued"
                )
            )
            forecast = None
            if state == "completed":
                forecast = models.read(
                    directory / f"image-{index:06d}" / "forecast.json", {"state": "pending"}
                )
            rows[photo] = {
                "id": photo,
                "job": child["id"],
                "index": index,
                "state": state,
                "device": result.get("device")
                or job.get("device")
                or (job.get("target_device") or {}).get("device"),
                "device_id": result.get("device_id")
                or job.get("device_id")
                or (job.get("target_device") or {}).get("id"),
                "hardware": result.get("hardware")
                or job.get("hardware")
                or (job.get("target_device") or {}).get("name"),
                "seconds": result.get("seconds"),
                "category": models.MODELS[job["model_id"]][0],
                "error": result.get("error") or job.get("error"),
                "forecast": forecast,
                "observed_utc": job["inputs"][index].get("observed_utc"),
            }
    if any((r["forecast"] or {}).get("state") == "pending" for r in rows.values()):
        models.ensure_worker(hub.out_dir, recover_forecasts=True)
    return {
        "id": identifier,
        "mode": batch.get("mode", "auto"),
        "rows": [rows[s["id"]] for s in batch["selection"]],
        "estimate": batch_estimate(hub, jobs),
    }


def retry_forecast(hub, project, identifier, photo):
    batch = status(hub, project, identifier)
    row = next((r for r in batch["rows"] if r["id"] == photo), None)
    if not row or (row["forecast"] or {}).get("state") not in ("failed", "unavailable"):
        raise ValueError("请选择需要重新匹配观测的照片")
    folder, _ = models._job(hub.out_dir, row["job"], hub)
    with models._lock:
        (folder / f"image-{row['index']:06d}" / "forecast.json").unlink(missing_ok=True)
        models.ensure_worker(hub.out_dir, recover_forecasts=True)
    return {"state": "pending"}


def archive(hub, project, identifier, only=""):
    batch = status(hub, project, identifier)
    _, folder = location(hub, project)
    photos = models.read(folder / "photos.json")
    rows = [r for r in batch["rows"] if not only or r["id"] == only]
    if not rows or any(r["state"] not in ("completed", "failed") for r in rows):
        raise ValueError("请等待所选照片处理完成")
    from .solar_input import original
    from .solar_products import regions
    import numpy as np

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as z:
        index = []
        coco = {
            "images": [],
            "annotations": [],
            "categories": [{"id": 1, "name": "AR"}, {"id": 2, "name": "CH"}],
        }
        for i, r in enumerate(rows, 1):
            photo = photos[r["id"]]
            entry = {**r, "name": photo["name"]}
            if r["state"] == "completed":
                job, idx = r["job"], r["index"]
                value = models.result(hub, hub.out_dir, job, idx, include_review=False)
                mask, _ = models.asset(hub, hub.out_dir, job, value["mask_id"])
                image, mime = original(hub, hub.out_dir, job, idx)
                prefix = f"{i:04d}/"
                entry["image"] = prefix + ("original.png" if mime == "image/png" else "original.jpg")
                entry["mask"] = prefix + "mask.png"
                z.writestr(entry["image"], image)
                z.writestr(entry["mask"], mask)
                with Image.open(io.BytesIO(mask)) as im:
                    components = regions(np.asarray(im))
                coco["images"].append(
                    {
                        "id": i,
                        "file_name": entry["image"],
                        "width": value["width"],
                        "height": value["height"],
                    }
                )
                for region in components:
                    coco["annotations"].append(
                        {
                            "id": len(coco["annotations"]) + 1,
                            "image_id": i,
                            "category_id": 1 if r["category"] == "AR" else 2,
                            "iscrowd": 1,
                            "area": region["area_pixels"],
                            "bbox": region["bbox"],
                            "segmentation": region["segmentation"],
                        }
                    )
                z.writestr(prefix + "regions.json", json.dumps(components))
                z.writestr(
                    prefix + "polygons.json", json.dumps(value["annotations"], ensure_ascii=False)
                )
            index.append(entry)
        z.writestr("coco.json", json.dumps(coco))
        z.writestr("results.json", json.dumps(index, ensure_ascii=False, indent=2))
    return stream.getvalue()
