"""Apply the frozen disk-wide model to one completed AR mask and lagged XRS."""

from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
from pathlib import Path

import numpy as np
from PIL import Image

from .durable import atomic_json
from .runtime import digest

HMI_IMAGE_SIZE = (1024, 1024)


@lru_cache(maxsize=2)
def _archive(folder, stamp, rows_stamp, weights_stamp):
    summary = json.loads((folder / "summary.json").read_text(encoding="utf8"))
    for name in ("rows", "weights"):
        if digest(folder / (name + ".json")) != summary[name + "_sha256"]:
            raise ValueError("预测资料校验失败")
    weights = json.loads((folder / "weights.json").read_text(encoding="utf8"))
    rows = json.loads((folder / "rows.json").read_text(encoding="utf8"))
    return (
        weights["xrs_plus_raw_plus_regions"],
        {r["source_sha256"]: r for r in rows},
        summary["weights_sha256"],
    )


@lru_cache(maxsize=2)
def _recent_xrs(minute):
    from .forecast_xrs_intake import PRIMARY, usable_minutes
    from mdva.web.space_weather import fetch

    raw, _ = fetch(PRIMARY)
    received = datetime.now(timezone.utc)
    return usable_minutes(json.loads(raw), received)[0], received.isoformat()


def evaluate(output, job, item, mask_path):
    from .forecast_disk_baseline import features
    from .forecast_disk_xrs import history, XRS

    if job["condition"] != "magnetogram":
        return {"state": "unavailable", "message": "耀斑试算适用于 HMI 活动区磁图"}
    observed = item.get("observed_utc")
    if not observed:
        return {"state": "unavailable", "message": "缺少观测时间，无法匹配 GOES 记录"}
    start = datetime.fromisoformat(observed)
    if start > datetime.now(timezone.utc):
        return {"state": "unavailable", "message": "观测时间晚于当前时刻，请核对输入"}
    folder = Path(output) / "forecast-supervision-20261116" / "full-disk-xrs-02"
    recipe, archive, version = _archive(
        folder,
        *((folder / (name + ".json")).stat().st_mtime_ns for name in ("summary", "rows", "weights")),
    )
    protocol = folder.parent / "full-disk-fitted-baseline-01" / "protocol.json"
    if json.loads(protocol.read_text(encoding="utf8"))["model_sha256"] != job["weights_sha256"]:
        raise ValueError("当前识别权重与预测基线不匹配")
    with Image.open(item["path"]) as im, Image.open(mask_path) as mask:
        if im.size != HMI_IMAGE_SIZE or mask.size != im.size:
            return {"state": "unavailable", "message": "试算需要原生 1024 × 1024 完整 HMI 磁图"}
        values = features(np.array(im.convert("L")), np.array(mask) > 0)
    row = archive.get(item["sha256"])
    now = datetime.now(timezone.utc)
    if row and datetime.fromisoformat(row["observed_utc"]) == start:
        values.update({key: row["features"][key] for key in XRS})
        xrs = row["xrs"]
        source = "GOES 已核验历史归档"
    elif timedelta(0) <= now - start < timedelta(days=6):
        series, received = _recent_xrs(int(now.timestamp() // 300))
        xrs = history(observed, series)
        if xrs is None:
            return {"state": "unavailable", "message": "GOES 有效分钟覆盖不足"}
        values.update(xrs["features"])
        source = "GOES 近实时观测 · " + received
    else:
        from .forecast_disk_xrs import series_for

        audit = folder.parent / "audit-04"
        index = audit / "coverage-index.json"
        if not index.exists():
            return {"state": "unavailable", "message": "本地缺少该时刻的已核验 GOES 记录"}
        if (
            digest(index)
            != json.loads((audit / "summary.json").read_text(encoding="utf8"))["coverage_index_sha256"]
        ):
            raise ValueError("GOES 分钟资料索引校验失败")
        series, _ = series_for(
            json.loads(index.read_text(encoding="utf8")),
            folder.parent / "sources",
            start.timestamp() - 25 * 3600,
            start.timestamp(),
        )
        xrs = history(observed, series)
        if xrs is None:
            return {"state": "unavailable", "message": "此时段的 GOES 有效分钟覆盖不足"}
        values.update(xrs["features"])
        source = "GOES 已核验历史分钟观测"
    vector = np.array([values[k] for k in recipe["features"]])
    logit = ((vector - recipe["mean"]) / recipe["scale"]) @ recipe["coef"] + recipe["intercept"]
    if not np.isfinite(logit):
        raise ValueError("预测输入异常")
    return {
        "state": "ready",
        "probability": float(1 / (1 + np.exp(-logit))),
        "observed_utc": observed,
        "end_utc": (start + timedelta(hours=24)).isoformat(),
        "source": source,
        "satellite": xrs["satellite"],
        "cutoff_utc": xrs["cutoff_utc"],
        "image_sha256": item["sha256"],
        "mask_sha256": digest(mask_path),
        "weights_sha256": version,
        "time_basis": item.get("time_basis"),
        "target": "全日面 M1.0 级及以上耀斑",
    }


def finish(output, folder, job):
    for row in job["results"]:
        if row["state"] != "completed":
            continue
        target = folder / f"image-{row['index']:06d}"
        path = target / "forecast.json"
        if path.exists():
            continue
        try:
            item = job["inputs"][row["index"]]
            mask = target / "candidate-mask.png"
            if digest(item["path"]) != item["sha256"] or digest(mask) != row["asset_hashes"][mask.name]:
                raise ValueError("原图或掩膜已变化")
            value = evaluate(output, job, item, mask)
        except Exception as exc:
            value = {"state": "failed", "message": "预测资料读取失败：" + str(exc)}
        atomic_json(path, value)
