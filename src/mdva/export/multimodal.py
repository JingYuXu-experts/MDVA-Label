"""多模态原生 JSON 导出（本设计的自描述格式）。

COCO/YOLO/VOC 只能装下图像框，时序异常区间、事件等级、立方体切片号、标注来源与
置信度全会在转换中丢掉。这个格式**无损**：五种模态都带自己的载荷摘要，时间既给
绝对 UTC 秒也给相对 t_ref 的秒数，每条标注保留 source/backend/confidence/state，
所以导出的数据集仍然可以回答"这条是谁标的、哪个算法给的、有没有复核过"。
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path

from .base import load_dataset, register, write_sample_image, write_sample_mask, write_report

SCHEMA = 1


def _ann_row(ann, meta: dict, t_ref) -> dict:
    row = {
        "geom": ann.geom, "coords": [round(v, 6) for v in ann.coords],
        "label": ann.label, "source": ann.source, "confidence": round(ann.confidence, 6),
        "backend": ann.backend, "slice": ann.slice, "level": ann.level,
        "color": list(ann.color) if ann.color else None, "note": ann.note,
        "state": meta.get("state", ""), "role": meta.get("role", ""),
        "annotation_id": meta.get("id", ""),
    }
    if ann.geom in ("interval", "level") and t_ref is not None:
        row["coords_local"] = [round(v - t_ref, 6) for v in ann.coords]
    return row


def _payload(sample, max_rows: int) -> dict:
    """数据项摘要：让导出包能脱离原始目录自检，也够论文画统计图。"""
    item = sample.item
    if item is None:
        return {}
    out = {"title": item.title, "dtype": item.dtype, "shape": list(item.shape or ()),
           "n_views": item.n_views, "meta": {k: v for k, v in (item.meta or {}).items()
                                             if k != "palette"}}
    try:
        data = item.load()
    except Exception as exc:
        out["load_error"] = str(exc)
        return out

    import numpy as np

    from ..core.model import Modality

    if item.modality == Modality.SERIES.value:
        out["channels"] = list(data.channels)
        out["units"] = list(data.units)
        out["discrete"] = list(data.discrete)
        out["n_samples"] = int(data.t.size)
        out["t_start"], out["t_end"] = float(data.start), float(data.end)
        with np.errstate(invalid="ignore"):
            out["value_min"] = [None if not np.isfinite(v) else round(float(v), 6)
                                for v in np.nanmin(data.values, axis=0)]
            out["value_max"] = [None if not np.isfinite(v) else round(float(v), 6)
                                for v in np.nanmax(data.values, axis=0)]
    elif item.modality == Modality.EVENT.value:
        out["n_events"] = len(data.rows)
        hist: dict = {}
        for r in data.rows:
            key = str(r.level)
            hist[key] = hist.get(key, 0) + 1
        out["level_histogram"] = hist
        out["rows"] = [{"t": r.t, "label": r.label, "level": r.level, "source": r.source,
                        "t_end": None if r.t_end == float("inf") else r.t_end}
                       for r in data.rows[:int(max_rows)]]
        out["rows_truncated"] = len(data.rows) > int(max_rows)
    return out


@register("multimodal", "mdva 多模态无损 JSON（五种模态 + 留痕）", {
    "copy_images": {"type": "boolean", "default": True, "description": "图像类样本一并落盘"},
    "include_payload": {"type": "boolean", "default": True, "description": "写时序/事件载荷摘要"},
    "max_event_rows": {"type": "integer", "default": 500},
    "states": {"type": "array", "default": None},
    "only_labels": {"type": "array", "default": None, "description": "类别白名单"},
    "min_confidence": {"type": "number", "default": None},
})
def export_multimodal(store, version_id, out_dir, copy_images=True, include_payload=True,
                      max_event_rows=500, states=None, min_confidence=None, only_labels=None,
                      root=None, **_ignored) -> dict:
    from .split import plan_split, summary, write_split_files

    ds = load_dataset(store, version_id, states=states, min_confidence=min_confidence,
                      only_labels=only_labels, root=root)
    out = Path(out_dir)
    assignment = plan_split(ds, seed=0)
    cat_ids = ds.category_ids()

    samples, n_files = [], 0
    for sample in ds.samples:
        entry = {
            "name": sample.name, "uri": sample.uri, "modality": sample.modality,
            "width": sample.width, "height": sample.height,
            "slice_index": sample.slice_index, "split": assignment.get(sample.name),
            "t_ref": sample.t_ref, "t_end": sample.t_end,
            "t_span_local": ([0.0, round(sample.t_end - sample.t_ref, 6)]
                             if sample.t_ref is not None and sample.t_end is not None else None),
            "annotations": [_ann_row(a, m, sample.t_ref) for a, m in sample.pairs()],
            "category_indices": [cat_ids.get(a.label or "(未分类)", cat_ids.get("(未分类)", 0))
                                 for a in sample.anns],
        }
        if include_payload:
            entry["payload"] = _payload(sample, max_event_rows)
        if copy_images and sample.is_spatial:
            placed = write_sample_image(sample, out / "images")
            entry["file_name"] = placed["file_name"]
            mask = write_sample_mask(sample, out / "masks") if sample.mask_uri else None
            if mask:
                entry["mask_file"] = mask["file_name"]
            n_files += 1
        samples.append(entry)

    payload = {
        "schema": SCHEMA,
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "format": "mdva-multimodal",
        "dataset": ds.dataset, "version": ds.version,
        "categories": [{"index": i, "name": n} for i, n in enumerate(ds.categories)],
        "labels": ds.labels,
        "timebase": {"unit": "utc_seconds", "note": "coords 为绝对 UTC 秒，coords_local 相对 t_ref"},
        "geoms": ["box", "poly", "point", "interval", "level"],
        "sources": sorted({a["source"] for s in samples for a in s["annotations"]}),
        "backends": sorted({a["backend"] for s in samples for a in s["annotations"] if a["backend"]}),
        "samples": samples,
        "stats": ds.stats(), "issues": ds.issues,
        "split": summary(assignment),
    }
    target = out / "mdva_dataset.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    index = _write_index(out / "annotations.csv", samples)
    write_split_files(out, assignment, [s["name"] for s in samples], subdir="splits")

    return write_report(out, ds, "multimodal", {
        "copy_images": copy_images, "include_payload": include_payload,
        "max_event_rows": max_event_rows, "states": states, "min_confidence": min_confidence,
        "only_labels": only_labels}, {
        "files": [target.name, index.name, "splits"],
        "n_images_copied": n_files,
        "n_annotations": sum(len(s["annotations"]) for s in samples),
        "n_categories": len(ds.categories),
        "split": summary(assignment),
    })


def _write_index(path: Path, samples: list) -> Path:
    """一张扁平表，方便用 Excel/pandas 直接审标注来源与状态分布。"""
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["sample", "modality", "slice", "uri", "geom", "label", "source",
                         "backend", "confidence", "state", "coords"])
        for s in samples:
            for a in s["annotations"]:
                state = a["state"] if isinstance(a["state"], str) else "|".join(a["state"])
                writer.writerow([s["name"], s["modality"], s["slice_index"], s["uri"],
                                 a["geom"], a["label"], a["source"], a["backend"],
                                 a["confidence"], state, json.dumps(a["coords"])])
    return path


__all__ = ["export_multimodal", "SCHEMA"]
