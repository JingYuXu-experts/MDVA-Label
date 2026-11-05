"""PASCAL VOC 导出。

VOC 只认矩形框，多边形额外挂一个 `<polygon>` 扩展节点（标准读方会忽略它，
不破坏兼容），点和时间区间仍然跳过并计数。bndbox 用 1 起闭区间整数，
与 labelme 转 VOC 的通行做法一致。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .base import (clamp_box, load_dataset, polygon_rings, register, write_sample_image,
                   write_sample_mask, write_report)


def _i(value) -> int:
    return int(round(float(value)))


def _build_xml(sample, placed, out_dir: Path, skipped: list, issues: list) -> ET.Element:
    root = ET.Element("annotation")
    ET.SubElement(root, "folder").text = "JPEGImages"
    ET.SubElement(root, "filename").text = placed["file_name"]
    ET.SubElement(root, "path").text = str(out_dir / "JPEGImages" / placed["file_name"])
    source = ET.SubElement(root, "source")
    ET.SubElement(source, "database").text = "mdva"
    size = ET.SubElement(root, "size")
    ET.SubElement(size, "width").text = str(int(placed["width"]))
    ET.SubElement(size, "height").text = str(int(placed["height"]))
    ET.SubElement(size, "depth").text = "3"
    ET.SubElement(root, "segmented").text = "0"

    meta = ET.SubElement(root, "mdva")
    ET.SubElement(meta, "uri").text = str(sample.uri)
    ET.SubElement(meta, "modality").text = str(sample.modality)
    ET.SubElement(meta, "slice").text = str(int(sample.slice_index))
    ET.SubElement(meta, "t_ref").text = "" if sample.t_ref is None else repr(sample.t_ref)

    w, h = int(placed["width"]), int(placed["height"])
    for ann in sample.anns:
        if ann.geom not in ("box", "poly"):
            skipped.append({"uri": sample.uri, "geom": ann.geom, "label": ann.label,
                            "reason": "VOC 只支持矩形框"})
            continue
        box = (clamp_box(ann, w, h, issues, sample) if ann.geom == "box"
               else _poly_bbox(ann, w, h, issues, sample))
        if box is None:
            skipped.append({"uri": sample.uri, "geom": ann.geom, "label": ann.label,
                            "reason": "裁到画外没有面积"})
            continue
        x, y, bw, bh = box
        obj = ET.SubElement(root, "object")
        ET.SubElement(obj, "name").text = ann.label or "(未分类)"
        ET.SubElement(obj, "pose").text = "Unspecified"
        ET.SubElement(obj, "truncated").text = "1" if _touches_edge(x, y, bw, bh, w, h) else "0"
        ET.SubElement(obj, "difficult").text = "1" if ann.confidence < 0.5 else "0"
        bnd = ET.SubElement(obj, "bndbox")
        ET.SubElement(bnd, "xmin").text = str(max(1, min(_i(x) + 1, w)))
        ET.SubElement(bnd, "ymin").text = str(max(1, min(_i(y) + 1, h)))
        ET.SubElement(bnd, "xmax").text = str(max(1, min(_i(x + bw), w)))
        ET.SubElement(bnd, "ymax").text = str(max(1, min(_i(y + bh), h)))
        attrs = ET.SubElement(obj, "attributes")
        ET.SubElement(attrs, "source").text = ann.source
        ET.SubElement(attrs, "backend").text = ann.backend or ""
        ET.SubElement(attrs, "score").text = f"{ann.confidence:.4f}"
        if ann.geom == "poly":
            ET.SubElement(obj, "polygon").text = _poly_text(polygon_rings(ann, w, h)[0])
    return root


def _poly_bbox(ann, w, h, issues, sample):
    xs, ys = ann.coords[0::2], ann.coords[1::2]
    x0, y0 = max(0.0, min(min(xs), w)), max(0.0, min(min(ys), h))
    x1, y1 = max(0.0, min(max(xs), w)), max(0.0, min(max(ys), h))
    if x1 - x0 < 1.0 or y1 - y0 < 1.0:
        issues.append({"kind": "box_out_of_bounds", "uri": sample.uri,
                       "slice": sample.slice_index, "coords": list(ann.coords),
                       "size": [w, h]})
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def _touches_edge(x, y, bw, bh, w, h) -> bool:
    return x <= 0.0 or y <= 0.0 or x + bw >= w - 1e-6 or y + bh >= h - 1e-6


def _poly_text(ring) -> str:
    return ";".join(f"{ring[i]:.2f},{ring[i + 1]:.2f}" for i in range(0, len(ring) - 1, 2))


@register("voc", "PASCAL VOC 2012 格式（矩形框 + polygon 扩展）", {
    "split_ratios": {"type": "string", "default": "0.7/0.15/0.15"},
    "seed": {"type": "integer", "default": 0},
    "include_masks": {"type": "boolean", "default": False},
    "states": {"type": "array", "default": None},
    "only_labels": {"type": "array", "default": None, "description": "类别白名单"},
    "min_confidence": {"type": "number", "default": None},
})
def export_voc(store, version_id, out_dir, split_ratios="0.7/0.15/0.15", seed=0,
               include_masks=False, states=None, min_confidence=None, only_labels=None,
               root=None, **_ignored) -> dict:
    from .split import plan_split, summary, write_split_files

    ds = load_dataset(store, version_id, states=states, min_confidence=min_confidence,
                      only_labels=only_labels, root=root)
    out = Path(out_dir)
    img_dir = out / "JPEGImages"
    ann_dir = out / "Annotations"
    ratios = [float(x) for x in str(split_ratios).split("/")]
    samples = ds.spatial()
    assignment = plan_split(ds, ratios=ratios, seed=int(seed))

    skipped: list = []
    stems = []
    for sample in samples:
        placed = write_sample_image(sample, img_dir)
        if include_masks:
            write_sample_mask(sample, img_dir.parent / "SegmentationClass")
        stem = Path(placed["file_name"]).stem
        stems.append(stem)
        xml_root = _build_xml(sample, placed, out, skipped, ds.issues)
        ET.indent(xml_root)
        ann_dir.mkdir(parents=True, exist_ok=True)
        ET.ElementTree(xml_root).write(str(ann_dir / f"{stem}.xml"),
                                       encoding="utf-8", xml_declaration=True)

    counts = write_split_files(out, assignment, stems)
    report = write_report(out, ds, "voc", {
        "split_ratios": split_ratios, "seed": seed, "include_masks": include_masks,
        "states": states, "min_confidence": min_confidence, "only_labels": only_labels}, {
        "files": ["Annotations", "JPEGImages", "ImageSets/Main"],
        "n_images": len(stems), "n_annotations": sum(len(s.anns) for s in samples) - len(skipped),
        "spatial_annotations": sum(len(s.anns) for s in samples),
        "n_skipped": len(skipped), "skipped": skipped,
        "split": summary(assignment), "split_files": counts,
    })
    return report


__all__ = ["export_voc"]
