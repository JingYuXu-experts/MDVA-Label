"""YOLO 导出（ultralytics 目录式布局）。

task=detect 写 `cls cx cy w h`，task=segment 写归一化多边形。两种任务的标签
不能混在一个目录里：ultralytics 按每行的数字个数决定当框还是当轮廓，
所以这里只选一种，另一种几何跳过并计数。
"""

from __future__ import annotations

from pathlib import Path

from .base import (clamp_box, load_dataset, normalized, polygon_rings, register,
                   write_sample_image, write_sample_mask, write_report)

FOLDS = ("train", "val", "test")


def _yaml(data: dict) -> str:
    """手写 YAML：只有一个平表和一个 names 映射，不值得为此引依赖。"""
    lines = []
    for key, value in data.items():
        if key == "names":
            lines.append("names:")
            for i, name in enumerate(value):
                lines.append(f"  {i}: {_quote(name)}")
        elif isinstance(value, (list, tuple)):
            lines.append(f"{key}: [{', '.join(_quote(v) for v in value)}]")
        else:
            lines.append(f"{key}: {_quote(value)}")
    return "\n".join(lines) + "\n"


def _quote(value) -> str:
    text = str(value)
    if text == "" or any(c in text for c in ":#{}[]&*!|>'\"%@`") or text != text.strip():
        return '"' + text.replace('"', '\\"') + '"'
    return text


@register("yolo", "YOLO 目录式数据集（detect / segment）", {
    "task": {"type": "string", "enum": ["detect", "segment"], "default": "detect"},
    "split_ratios": {"type": "string", "default": "0.7/0.15/0.15"},
    "seed": {"type": "integer", "default": 0},
    "include_background": {"type": "boolean", "default": False,
                           "description": "无标注图片也写空的标签文件"},
    "include_masks": {"type": "boolean", "default": False},
    "states": {"type": "array", "default": None},
    "only_labels": {"type": "array", "default": None, "description": "类别白名单"},
    "min_confidence": {"type": "number", "default": None},
})
def export_yolo(store, version_id, out_dir, task="detect", split_ratios="0.7/0.15/0.15",
                seed=0, include_background=False, include_masks=False, states=None,
                min_confidence=None, only_labels=None, root=None, **_ignored) -> dict:
    from .split import plan_split, summary

    if task not in ("detect", "segment"):
        raise ValueError(f"未知任务 {task!r}，可用 detect / segment")
    ds = load_dataset(store, version_id, states=states, min_confidence=min_confidence,
                      only_labels=only_labels, root=root)
    out = Path(out_dir)
    ratios = [float(x) for x in str(split_ratios).split("/")]
    samples = ds.spatial()
    assignment = plan_split(ds, ratios=ratios, seed=int(seed))
    cat_ids = ds.category_ids()

    skipped: list = []
    written = {f: 0 for f in FOLDS}
    background = {f: 0 for f in FOLDS}
    lines_total = 0
    for sample in samples:
        fold = assignment.get(sample.name, FOLDS[0])
        placed = write_sample_image(sample, out / "images" / fold)
        if include_masks:
            write_sample_mask(sample, out / "masks" / fold)
        rows = _label_lines(sample, placed, cat_ids, task, ds.issues, skipped)
        label_dir = out / "labels" / fold
        label_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(placed["file_name"]).stem
        if rows:
            (label_dir / f"{stem}.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")
            written[fold] += 1
            lines_total += len(rows)
        elif include_background:
            (label_dir / f"{stem}.txt").write_text("", encoding="utf-8")
            background[fold] += 1

    yaml_path = out / "data.yaml"
    yaml_path.write_text(_yaml({
        "path": str(out.resolve()).replace("\\", "/"),
        "train": "images/train", "val": "images/val", "test": "images/test",
        "nc": len(ds.categories), "names": list(ds.categories),
        "task": task, "mdva_dataset": ds.dataset.get("name"),
        "mdva_version": ds.version.get("id"),
    }), encoding="utf-8")

    report = write_report(out, ds, "yolo", {
        "task": task, "split_ratios": split_ratios, "seed": seed,
        "include_background": include_background, "include_masks": include_masks,
        "states": states, "min_confidence": min_confidence, "only_labels": only_labels}, {
        "files": ["data.yaml"],
        "n_images": sum(written.values()) + sum(background.values()),
        "n_labels_per_fold": written, "n_background_per_fold": background,
        "n_annotations": lines_total, "spatial_annotations": sum(len(s.anns) for s in samples),
        "n_skipped": len(skipped), "skipped": skipped,
        "n_categories": len(ds.categories), "split": summary(assignment),
    })
    return report


def _label_lines(sample, placed, cat_ids: dict, task: str, issues: list, skipped: list):
    w, h = int(placed["width"]), int(placed["height"])
    rows = []
    for ann in sample.anns:
        cls = cat_ids.get(ann.label or "(未分类)", cat_ids.get("(未分类)", 0))
        if ann.geom == "box":
            box = clamp_box(ann, w, h, issues, sample)
            if box is None:
                skipped.append({"uri": sample.uri, "geom": ann.geom, "label": ann.label,
                                "reason": "矩形裁完没有面积"})
                continue
            x, y, bw, bh = box
            if task == "detect":
                rows.append(" ".join([str(cls)] + [f"{v:.6f}" for v in
                            _mid(x, y, bw, bh, w, h)]))
            else:
                poly = [x, y, x + bw, y, x + bw, y + bh, x, y + bh]
                rows.append(" ".join([str(cls)] + [f"{v:.6f}" for v in
                            normalized(poly, w, h)]))
            continue
        if ann.geom == "poly":
            if task != "segment":
                skipped.append({"uri": sample.uri, "geom": "poly", "label": ann.label,
                                "reason": "detect 任务不写轮廓，改 task=segment"})
                continue
            ring = polygon_rings(ann, w, h)[0]
            if _collapsed(ring):
                skipped.append({"uri": sample.uri, "geom": "poly", "label": ann.label,
                                "reason": "多边形退化"})
                continue
            rows.append(" ".join([str(cls)] + [f"{v:.6f}" for v in normalized(ring, w, h)]))
            continue
        skipped.append({"uri": sample.uri, "geom": ann.geom, "label": ann.label,
                        "reason": "YOLO 标签只表示空间几何"})
    return rows


def _mid(x, y, bw, bh, w, h):
    return normalized([x + bw / 2.0, y + bh / 2.0, bw, bh], w, h)


def _collapsed(ring) -> bool:
    xs, ys = ring[0::2], ring[1::2]
    return max(xs) - min(xs) <= 0.0 or max(ys) - min(ys) <= 0.0


__all__ = ["export_yolo"]
