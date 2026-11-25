import base64
import hashlib
import io
import uuid
from pathlib import Path

import pytest
from PIL import Image

from mdva.training.durable import atomic_json
from mdva.web import solar_models as models, solar_projects as projects
from mdva.web.services import Hub


@pytest.fixture
def work(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    hub = Hub(root / "mdva.sqlite", out_dir=tmp_path / "out", data_root=root)
    exp = tmp_path / "experiment"
    monkeypatch.setattr(models, "experiment", lambda _: exp)
    monkeypatch.setattr(projects, "input_identity", lambda *args: {})
    monkeypatch.setattr(models, "ensure_worker", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        models,
        "inventory",
        lambda _: {
            "models": [
                {"id": identifier, "condition": condition, "available": True, "weights_sha256": "test"}
                for identifier, (_, condition) in models.MODELS.items()
            ]
        },
    )
    from mdva.training.solar_device import CPU

    monkeypatch.setattr(projects, "hardware", lambda _: [CPU])
    yield hub, projects.create(hub, "观测")["id"]
    hub.close()


@pytest.mark.parametrize(
    "available,cap,expected", [(False, (8, 6), "cpu"), (True, (5, 2), "cpu"), (True, (8, 9), "cuda")]
)
def test_device_selection_respects_compiled_kernels(available, cap, expected):
    from types import SimpleNamespace
    from mdva.training.solar_device import automatic, inventory

    cuda = SimpleNamespace(
        is_available=lambda: available,
        current_device=lambda: 0,
        device_count=lambda: 1,
        get_device_capability=lambda _: cap,
        get_arch_list=lambda: ["sm_70", "sm_86"],
        get_device_name=lambda _: "test GPU",
    )
    assert automatic(inventory(cuda))["device"] == expected


def test_device_selection_finds_a_compatible_second_gpu():
    from types import SimpleNamespace
    from mdva.training.solar_device import automatic, inventory

    cuda = SimpleNamespace(
        is_available=lambda: True,
        current_device=lambda: 0,
        device_count=lambda: 2,
        get_device_capability=lambda index: [(5, 2), (8, 9)][index],
        get_arch_list=lambda: ["sm_86"],
        get_device_name=lambda index: ["legacy", "compatible"][index],
    )
    devices = inventory(cuda)
    assert not devices[1]["available"]
    assert automatic(devices)["id"] == "cuda:1"


def image(color, name="日面.png", size=(256, 256)):
    stream = io.BytesIO()
    Image.new("L", size, color).save(stream, "PNG")
    return {
        "filename": name,
        "image_data": "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode(),
    }


def test_import_preserves_names_deduplicates_bytes_and_keeps_projects_separate(work):
    hub, project = work
    first = projects.import_photo(hub, project, image(1))
    assert not first["duplicate"]
    assert projects.import_photo(hub, project, image(1, "重新命名.png"))["duplicate"]
    second = projects.import_photo(hub, project, image(2))
    assert first["photo"]["id"] != second["photo"]["id"]
    assert len(projects.catalog(hub, project)["photos"]) == 2
    assert not projects.catalog(hub, project)["batches"]
    other = projects.create(hub, "第二个项目")["id"]
    with pytest.raises(ValueError, match="不属于"):
        projects.photo_asset(hub, other, first["photo"]["id"], False)
    assert projects.photo_asset(hub, project, first["photo"]["id"], True)[1] == "image/jpeg"


def test_mixed_confirmed_selection_keeps_order_and_is_idempotent(work):
    hub, project = work
    ids = [projects.import_photo(hub, project, image(i))["photo"]["id"] for i in range(3)]
    picked = [{"id": ids[i], "category": category} for i, category in [(2, "CH"), (0, "AR")]]
    identifier = uuid.uuid4().hex
    projects.submit(hub, project, identifier, picked)
    projects.submit(hub, project, identifier, picked)
    rows = projects.status(hub, project, identifier)["rows"]
    assert [r["id"] for r in rows] == [ids[2], ids[0]]
    assert len({r["job"] for r in rows}) == 2
    assert len(list(models.job_root(hub.out_dir).glob("*/job.json"))) == 2
    with pytest.raises(ValueError, match="已变化"):
        projects.submit(hub, project, identifier, picked[::-1])
    with pytest.raises(ValueError):
        projects.submit(hub, project, uuid.uuid4().hex, [{"id": ids[1], "category": ""}])
    assert len(projects.catalog(hub, project)["batches"]) == 1


def test_large_batch_recovers_after_partial_dispatch(work, monkeypatch):
    hub, project = work
    dataset, folder = projects.location(hub, project)
    photos = {}
    for i in range(201):
        data = str(i).encode()
        key = hashlib.sha256(data).hexdigest()
        path = Path(dataset["root"]) / (key + ".png")
        path.write_bytes(data)
        photos[key] = {"id": key, "category": "AR", "observed_utc": None, "uri": hub.store.to_rel(path)}
    atomic_json(folder / "photos.json", photos)
    picked = [{"id": key, "category": "AR"} for key in photos]
    identifier = uuid.uuid4().hex
    enqueue = models.enqueue

    def interrupt(*args, **kwargs):
        if len(args[4]) == 1:
            raise OSError("interrupted")
        return enqueue(*args, **kwargs)

    monkeypatch.setattr(models, "enqueue", interrupt)
    with pytest.raises(OSError):
        projects.submit(hub, project, identifier, picked)
    monkeypatch.setattr(models, "enqueue", enqueue)
    rows = projects.status(hub, project, identifier)["rows"]
    assert [r["id"] for r in rows] == list(photos)
    assert len({r["job"] for r in rows[:200]}) == 1
    assert rows[-1]["job"] != rows[0]["job"]
    assert len(hub.store.list_tasks(project)) == 2


def test_import_without_model_can_browse_but_cannot_submit_unchecked(work, monkeypatch):
    hub, project = work
    identity_check = projects.input_identity

    def missing(*args):
        raise FileNotFoundError("the deployment manifest is missing")

    monkeypatch.setattr(projects, "input_identity", missing)
    photo = projects.import_photo(hub, project, image(8))["photo"]
    assert photo["category"] == ""
    assert "pixel_hashes" not in photo
    assert projects.photo_asset(hub, project, photo["id"], False)[1] == "image/png"
    selection = [{"id": photo["id"], "category": "AR"}]
    with pytest.raises(ValueError, match="缺少识别模型"):
        projects.submit(hub, project, uuid.uuid4().hex, selection)
    assert not projects.catalog(hub, project)["batches"]

    def check_id(exp, key, pixels):
        assert key == photo["id"] and pixels
        return identity_check(exp, key, pixels)

    monkeypatch.setattr(projects, "input_identity", check_id)
    projects.submit(hub, project, uuid.uuid4().hex, selection)
    assert len(projects.catalog(hub, project)["batches"]) == 1


def test_import_registered_png_uses_its_image_format(work, monkeypatch):
    from mdva.web.solar_catalog import SolarCatalog

    hub, project = work
    payload = base64.b64decode(image(16)["image_data"].split(",", 1)[1])
    monkeypatch.setattr(SolarCatalog, "image", lambda self, uri: payload)
    photo = projects.import_photo(hub, project, {"catalog_uri": "observation.png"})["photo"]
    original, mime = projects.photo_asset(hub, project, photo["id"], False)
    assert original == payload and mime == "image/png"


def test_device_choices_are_persisted_and_photo_order_is_kept(work, monkeypatch):
    from mdva.training.solar_device import CPU

    hub, project = work
    gpus = [
        {"id": f"cuda:{i}", "device": "cuda", "name": f"GPU {i}", "index": i, "available": True}
        for i in range(2)
    ]
    monkeypatch.setattr(projects, "hardware", lambda _: [CPU, *gpus])
    selection = [
        {"id": projects.import_photo(hub, project, image(i))["photo"]["id"], "category": "AR"}
        for i in range(4)
    ]
    for mode, expected in [("cpu", {"cpu"}), ("cuda:1", {"cuda:1"}), ("all-gpus", {"cuda:0", "cuda:1"})]:
        batch = uuid.uuid4().hex
        projects.submit(hub, project, batch, selection, mode)
        status = projects.status(hub, project, batch)
        assert status["mode"] == mode
        assert [r["id"] for r in status["rows"]] == [r["id"] for r in selection]
        assert {r["device_id"] for r in status["rows"]} == expected
        assert status["estimate"]["seconds"] is None  # No device timing samples yet.
        with pytest.raises(ValueError, match="处理设备已变化"):
            projects.submit(hub, project, batch, selection, "auto")
    with pytest.raises(ValueError, match="所选处理设备不可用"):
        projects.submit(hub, project, uuid.uuid4().hex, selection, "cuda:5")


def test_existing_photo_grid_has_scoped_thumbnails_and_bulk_import(work):
    hub, _ = work
    originals = hub.path.parent / "collection"
    originals.mkdir()
    for number in range(63):
        Image.new("L", (260, 260), number).save(originals / f"observed-{number:02d}.png")
    existing = hub.create_dataset("Existing", "segmentation", originals)["dataset_id"]
    first = projects.existing_images(hub, existing, "observed", 1, refresh=True)
    second = projects.existing_images(hub, existing, "observed", 2)
    assert (first["total"], len(first["images"]), len(second["images"])) == (63, 60, 3)
    assert len(projects.existing_images(hub, existing, "observed-6", 1, ids_only=True)["ids"]) == 3
    left, right = (page["images"][0]["id"] for page in (first, second))
    with Image.open(io.BytesIO(projects.source_thumbnail(hub, existing, right))) as thumb:
        assert thumb.format == "JPEG"
        assert max(thumb.size) == 240
    assert not projects.catalog(hub, existing)["photos"]
    with pytest.raises(ValueError, match="重新打开列表"):
        projects.source_thumbnail(hub, work[1], left)
    report = projects.import_sources(hub, existing, [left, "absent", right])["results"]
    assert [row["id"] for row in report] == [left, "absent", right]
    assert report[0]["name"].endswith(".png")
    assert report[1]["error"]
    assert "photo" in report[0] and "photo" in report[2]
    assert len(projects.catalog(hub, existing)["photos"]) == 2
    assert projects.import_sources(hub, existing, [left])["results"][0]["duplicate"]


def test_project_preferences_persist_open_counts_and_pin(work):
    hub, project = work
    projects.update_project(hub, project, "open")
    projects.update_project(hub, project, "open")
    pinned = projects.update_project(hub, project, "pin", True)
    assert pinned["pinned"] and pinned["opens"] == 2 and pinned["opened_at"]
    assert next(p for p in projects.projects(hub) if p["id"] == project) == pinned
    assert not projects.update_project(hub, project, "pin", False)["pinned"]


def test_targeted_precheck_counts_inputs_and_enforces_the_same_rules_at_submission(work, monkeypatch):
    from collections import Counter

    hub, project = work
    metadata = {}
    monkeypatch.setattr(projects, "input_identity", lambda _, key, __: metadata.get(key, {}))
    when = "2024-01-01T00:05:00+00:00"
    inputs = [
        ("valid", (1024, 1024), "HMI", when),
        ("too-small", (512, 512), "HMI", when),
        ("unknown-time", (1024, 1024), "HMI", None),
        ("euv", (512, 512), "AIA193", when),
        ("other-aia", (512, 512), "AIA304", when),
        ("hmi-name-only", (1024, 1024), None, None),
        ("outside", (1024, 1024), "HMI", "2024-01-02T00:00:00Z"),
        ("changed", (512, 512), "AIA193", when),
        ("other-instrument", (512, 512), "MDI", when),
    ]
    photos = {}
    for i, (name, size, instrument, date) in enumerate(inputs):
        params = image(i + 1, name + ".png", size)
        key = hashlib.sha256(base64.b64decode(params["image_data"].split(",")[1])).hexdigest()
        if instrument:
            metadata[key] = {"instrument": instrument, "time_utc": date}
        photos[name] = projects.import_photo(hub, project, params)["photo"]["id"]
    _, folder = projects.location(hub, project)
    stored = models.read(folder / "photos.json")
    hub.store.require_within(stored[photos["changed"]]["uri"]).write_bytes(b"changed")

    selected = [{"id": key} for key in photos.values()]
    bounds = {"start_utc": "2024-01-01T08:05:00+08:00", "end_utc": "2024-01-01T08:05:00+08:00"}
    ar = projects.precheck(hub, project, selected, "flare-risk", bounds)
    assert Counter(row["status"] for row in ar["photos"]) == {
        "eligible": 1,
        "unsupported": 4,
        "pending": 2,
        "mismatch": 1,
        "outside_time": 1,
    }
    assert Counter(row["data_type"] for row in ar["photos"]) == {
        "hmi": 4,
        "aia": 3,
        "other": 1,
        "unknown": 1,
    }
    assert ar["selection"] == [{"id": photos["valid"], "category": "AR", "observed_utc": when}]
    ch = projects.precheck(hub, project, selected, "coronal-hole")
    assert [row["id"] for row in ch["selection"]] == [photos["euv"]]
    assert {row["id"]: row["status"] for row in ch["photos"]}[photos["valid"]] == "mismatch"
    assert not projects.catalog(hub, project)["batches"]

    identifier = uuid.uuid4().hex
    for name, requested in [
        ("euv", "AR"),
        ("hmi-name-only", ""),
        ("too-small", "AR"),
        ("outside", "AR"),
    ]:
        invalid = {"id": photos[name], "category": requested}
        with pytest.raises(ValueError, match="入队校验"):
            projects.submit(
                hub, project, identifier, ar["selection"] + [invalid], target="flare-risk", window=bounds
            )
    with pytest.raises(ValueError, match="没有符合"):
        projects.submit(hub, project, identifier, [], target="flare-risk")
    assert not projects.catalog(hub, project)["batches"]
    assert not hub.store.list_tasks(project)

    projects.submit(hub, project, identifier, ar["selection"], "cpu", target="flare-risk", window=bounds)
    rows = projects.status(hub, project, identifier)["rows"]
    assert [(row["id"], row["category"], row["observed_utc"]) for row in rows] == [
        (photos["valid"], "AR", when)
    ]

    overrides = list(selected)
    overrides[5] = {
        "id": photos["hmi-name-only"],
        "category": "AR",
        "observed_utc": "2023-12-31T19:05:00-05:00",
    }
    manual = projects.precheck(hub, project, overrides, "flare-risk", bounds)
    assert {row["id"] for row in manual["selection"]} == {photos["valid"], photos["hmi-name-only"]}
    assert photos["hmi-name-only"] not in {
        row["id"] for row in projects.precheck(hub, project, overrides, "coronal-hole")["selection"]
    }
    assert projects.catalog(hub, project)["photos"][5]["category"] == ""


def test_targeted_precheck_rejects_missing_target_and_invalid_utc_bounds(work):
    hub, project = work
    photo = projects.import_photo(hub, project, image(2))["photo"]["id"]
    with pytest.raises(ValueError, match="观测时间须"):
        projects.precheck(
            hub, project, [{"id": photo}], "coronal-hole", {"start_utc": "2024-01-01T10:00:00"}
        )
    with pytest.raises(ValueError, match="不能晚于"):
        projects.precheck(
            hub,
            project,
            [{"id": photo}],
            "coronal-hole",
            {"start_utc": "2024-01-01T10:00:00Z", "end_utc": "2024-01-01T09:00:00Z"},
        )
    with pytest.raises(ValueError, match="请先选择识别目标"):
        projects.precheck(hub, project, [{"id": photo}], "")


def test_project_precheck_and_batch_routes_require_a_goal(work):
    from types import SimpleNamespace
    from mdva.web.routes import call

    hub, project = work
    photo = projects.import_photo(hub, project, image(3))["photo"]["id"]
    ws = SimpleNamespace(hub=lambda _: hub)
    request = {"project": project, "selection": [{"id": photo, "category": "CH"}]}
    batch = {**request, "id": uuid.uuid4().hex, "device": "cpu"}
    assert call(ws, "POST", "/api/solar/project/batch", body=batch).status == 400
    review = call(
        ws, "POST", "/api/solar/project/batch/precheck", body={**request, "goal": "coronal-hole"}
    )
    assert review.status == 200 and len(review.json_body["selection"]) == 1
    batch["selection"] = review.json_body["selection"]
    wrong = call(ws, "POST", "/api/solar/project/batch", body={**batch, "goal": "flare-risk"})
    assert wrong.status == 400
    assert not hub.store.list_tasks(project)
    accepted = call(ws, "POST", "/api/solar/project/batch", body={**batch, "goal": "coronal-hole"})
    assert accepted.status == 200
    assert len(hub.store.list_tasks(project)) == 1
