"""COCO instances 导出。

只导出 COCO 真能表达的几何（box / poly）。点和时间区间不属于 instances 语义，
硬塞成零面积框会让任何读这个 json 的框架算出 0/0 —— 所以明确跳过并计数，
校验器再检查"写出数 + 跳过数 = 库里总数"这条账必须平。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .base import (clamp_box, load_dataset, polygon_rings, register, write_sample_image,
                   write_sample_mask, write_report)

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _polygon_area(coords) -> float:
    """多边形用鞋带公式：COCO 的 area 是分割像素面积，取外接框会高估好几倍。"""
    xs, ys = coords[0::2], coords[1::2]
    s = 0.0
    for i in range(len(xs)):
        j = (i + 1) % len(xs)
        s += xs[i] * ys[j] - xs[j] * ys[i]
    return abs(s) / 2.0


def _captured(sample) -> str:
    if sample.t_ref is None:
        return ""
    return dt.datetime.utcfromtimestamp(sample.t_ref).strftime(TIME_FORMAT)


@register("coco", "COCO instances 检测/分割格式（box + poly）", {
    "images_dir": {"type": "string", "default": "images", "description": "图片子目录"},
    "split_ratios": {"type": "string", "default": "0.7/0.15/0.15",
                     "description": "train/val/test 比例"},
    "seed": {"type": "integer", "default": 0},
    "segmentation": {"type": "boolean", "default": True,
                     "description": "box 是否也写成多边形轮廓（分割训练需要）"},
    "include_masks": {"type": "boolean", "default": False,
                      "description": "有配对掩膜时把类别编号掩膜一起复制过来"},
    "states": {"type": "array", "default": None, "description": "只导出这些审核状态，默认全部"},
    "only_labels": {"type": "array", "default": None, "description": "类别白名单，用于按类导出"},
    "min_confidence": {"type": "number", "default": None},
})
def export_coco(store, version_id, out_dir, images_dir="images", split_ratios="0.7/0.15/0.15",
                seed=0, segmentation=True, include_masks=False, states=None,
                min_confidence=None, only_labels=None, root=None, **_ignored) -> dict:
    from .split import plan_split, summary

    ds = load_dataset(store, version_id, states=states, min_confidence=min_confidence,
                      only_labels=only_labels, root=root)
    out = Path(out_dir)
    img_dir = out / images_dir
    img_dir.mkdir(parents=True, exist_ok=True)

    ratios = [float(x) for x in str(split_ratios).split("/")]
    samples = ds.spatial()
    assignment = plan_split(ds, ratios=ratios, seed=int(seed))

    images, annotations, skipped = [], [], []
    for sample in samples:
        placed = write_sample_image(sample, img_dir)
        if include_masks:
            # 掩膜单独放一层：images/ 里混进类别编号图，任何 glob 它的训练脚本都会当成样本
            write_sample_mask(sample, out / "masks")
        cat = ds.category_ids()
        for ann, meta in sample.pairs():
            body, why = _annotation(ann, sample, placed, cat, ds.issues)
            if body is None:
                skipped.append({"uri": sample.uri, "slice": sample.slice_index,
                                "geom": ann.geom, "label": ann.label, "reason": why})
                continue
            body["id"] = len(annotations) + 1
            body["image_id"] = len(images) + 1
            body["mdva"] = {"uri": sample.uri, "slice": sample.slice_index,
                            "source": ann.source, "backend": ann.backend,
                            "confidence": ann.confidence, "state": meta["state"],
                            "annotation_id": meta["id"]}
            annotations.append(body)
        images.append({"id": len(images) + 1, "file_name": placed["file_name"],
                       "width": int(placed["width"]), "height": int(placed["height"]),
                       "license": 0, "date_captured": _captured(sample),
                       "coco_url": "", "flickr_url": "",
                       "mdva": {"uri": sample.uri, "modality": sample.modality,
                                "slice": sample.slice_index,
                                "split": assignment.get(sample.name),
                                "mask": bool(sample.mask_uri)}})

    payload_all = _wrap(ds, images, annotations)
    files = [_dump(out / "instances.json", payload_all)]

    fold_names = {}
    for fold in ("train", "val", "test"):
        keep = {e["id"] for e in images if e["mdva"]["split"] == fold}
        fold_images = [e for e in images if e["id"] in keep]
        fold_anns = [a for a in annotations if a["image_id"] in keep]
        files.append(_dump(out / f"instances_{fold}.json", _wrap(ds, fold_images, fold_anns)))
        fold_names[fold] = {"images": len(fold_images), "annotations": len(fold_anns)}

    report = write_report(out, ds, "coco", {
        "images_dir": images_dir, "split_ratios": split_ratios, "seed": seed,
        "segmentation": segmentation, "include_masks": include_masks, "states": states,
        "min_confidence": min_confidence, "only_labels": only_labels}, {
        "files": [str(Path(f).name) for f in files],
        "n_images": len(images), "n_annotations": len(annotations),
        "n_categories": len(payload_all["categories"]),
        "n_skipped": len(skipped), "skipped": skipped,
        "spatial_annotations": sum(len(s.anns) for s in samples),
        "split": summary(assignment), "per_split": fold_names,
    })
    return report


def _wrap(ds, images, annotations) -> dict:
    categories = []
    for i, name in enumerate(ds.categories):
        entry = {"id": i + 1, "name": name, "supercategory": "none"}
        for lab in ds.labels:
            if lab["name"] == name and lab.get("color"):
                entry["color"] = lab["color"]
                entry["hotkey"] = lab.get("hotkey", "")
        categories.append(entry)
    return {
        "info": {"description": f"mdva/{ds.dataset.get('name')} @ {ds.version.get('id')}",
                 "version": "1.0", "mdva_schema": 1,
                 "date_created": dt.datetime.now().strftime(TIME_FORMAT)},
        "licenses": [], "images": images, "annotations": annotations,
        "categories": categories,
    }


def _annotation(ann, sample, placed, cat_ids: dict, issues: list):
    """返回 (coco 标注, 跳过原因)。二者只有一个非空。"""
    w, h = int(placed["width"]), int(placed["height"])
    category = cat_ids.get(ann.label or "(未分类)", cat_ids.get("(未分类)", 0)) + 1
    common = {"category_id": category, "iscrowd": 0,
              "attributes": {"source": ann.source, "score": ann.confidence,
                             "level": ann.level, "slice": ann.slice}}
    if ann.geom == "box":
        box = clamp_box(ann, w, h, issues, sample)
        if box is None:
            return None, "矩形裁完没有面积"
        x, y, bw, bh = box
        return ({**common, "bbox": [round(v, 3) for v in (x, y, bw, bh)],
                 "area": round(bw * bh, 3),
                 "segmentation": [[x, y, x + bw, y, x + bw, y + bh, x, y + bh]]}, None)
    if ann.geom == "poly":
        rings = polygon_rings(ann, w, h)
        xs, ys = ann.coords[0::2], ann.coords[1::2]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        if x1 - x0 < 1.0 or y1 - y0 < 1.0:
            return None, "多边形退化"
        return ({**common, "bbox": [round(v, 3) for v in (x0, y0, x1 - x0, y1 - y0)],
                 "area": round(_polygon_area(ann.coords), 3), "segmentation": rings}, None)
    if ann.geom == "point":
        return None, "COCO instances 不表示点"
    return None, "时间类几何不属于图像标注"


def _dump(path: Path, payload: dict) -> str:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


__all__ = ["export_coco"]
