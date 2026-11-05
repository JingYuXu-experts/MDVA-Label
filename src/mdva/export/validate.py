"""导出回读校验：证明"落盘的数据集"和"数据库里的版本"是同一批东西。

每种格式都查三件事：结构合法（字段齐、坐标在画内、编号能对上外键）、
数量守恒（写出 + 明确跳过 = 库里该导出的条数）、文件真的存在。
第三条账最容易被忽略，也最要命——静默丢标注的导出集看着漂亮，训出来的模型却少了两类。
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from ..annotate.model import GEOMS
from .base import read_report

MAX_ISSUES = 50


def validate(path, version_id: str, store, states=None, root=None,
             expectation: Optional[dict] = None) -> dict:
    out = Path(path)
    report = read_report(out)
    result = {"path": str(out), "format": report.get("format"), "version": report.get("version"),
              "ok": True, "issues": [], "checked": {}, "counts": {}}
    if report.get("version") != version_id:
        _issue(result, f"导出目录属于版本 {report.get('version')}，不是 {version_id}")

    fmt = (report.get("format") or "").lower()
    checker = {"coco": _coco, "yolo": _yolo, "voc": _voc, "multimodal": _multimodal}.get(fmt)
    if checker is None:
        raise ValueError(f"未知导出格式 {fmt!r}，可用 coco / yolo / voc / multimodal")
    checker(out, report, result)

    if store is not None and expectation is None:
        params = report.get("params") or {}
        from .base import load_dataset

        ds = load_dataset(store, version_id,
                          states=states or params.get("states"),
                          min_confidence=params.get("min_confidence"),
                          only_labels=params.get("only_labels"), root=root)
        expectation = _expectation(ds)
    if expectation:
        _reconcile(result, report, expectation)
        result["counts"]["expected"] = expectation

    result["ok"] = not result["issues"]
    return result


def _expectation(ds) -> dict:
    rows = [a for s in ds.samples for a in s.anns]
    return {"n_samples": len(ds.samples), "n_annotations": len(rows),
            "spatial_samples": len(ds.spatial()),
            "spatial_annotations": sum(len(s.anns) for s in ds.spatial()),
            "time_annotations": sum(len(s.anns) for s in ds.time_samples()),
            "n_categories": len(ds.categories),
            "geoms": {g: sum(1 for a in rows if a.geom == g) for g in GEOMS}}


# ---------------------------------------------------------------------- 守恒

def _reconcile(result: dict, report: dict, exp: dict) -> None:
    """数量守恒：写出的 + 明确跳过的 = 库里该导出的，一条都不能凭空消失。"""
    written = result["counts"].get("written", 0)
    skipped = report.get("n_skipped", 0)
    target = exp["spatial_annotations"] if report.get("format") != "multimodal" \
        else exp["n_annotations"]
    result["checked"]["conservation"] = {"written": written, "skipped": skipped, "target": target}
    if written + skipped != target:
        _issue(result, f"数量不守恒：写出 {written} + 跳过 {skipped} != 应导出 {target}")
    if report.get("spatial_annotations") not in (None, exp["spatial_annotations"]):
        _issue(result, f"导出时的空间标注数 {report.get('spatial_annotations')} "
                       f"与当前库中 {exp['spatial_annotations']} 不一致（版本被改动过？）")
    if report.get("n_categories") not in (None, exp["n_categories"]):
        _issue(result, f"类别数不一致：导出 {report.get('n_categories')} vs 库中 {exp['n_categories']}")


# ---------------------------------------------------------------------- COCO

def _coco(out: Path, report: dict, result: dict) -> None:
    data = _load_json(out / "instances.json", result)
    if data is None:
        return
    for key in ("info", "images", "annotations", "categories"):
        if key not in data:
            _issue(result, f"instances.json 缺少顶层字段 {key}")
    images = data.get("images") or []
    annotations = data.get("annotations") or []
    cats = data.get("categories") or []
    img_by_id = {e.get("id"): e for e in images}
    if len(img_by_id) != len(images):
        _issue(result, "images.id 有重复")
    cat_ids = {c.get("id") for c in cats}
    ann_ids = [a.get("id") for a in annotations]
    if len(set(ann_ids)) != len(ann_ids):
        _issue(result, "annotations.id 有重复")

    for a in annotations:
        img = img_by_id.get(a.get("image_id"))
        if img is None:
            _issue(result, f"标注 {a.get('id')} 的 image_id={a.get('image_id')} 无对应图片")
            continue
        if a.get("category_id") not in cat_ids:
            _issue(result, f"标注 {a.get('id')} 的 category_id 不在类别表里")
        bbox = a.get("bbox") or []
        if len(bbox) != 4 or not all(isinstance(v, (int, float)) for v in bbox):
            _issue(result, f"标注 {a.get('id')} 的 bbox 不是四个数：{bbox}")
            continue
        x, y, w, h = bbox
        if w <= 0 or h <= 0 or x < 0 or y < 0:
            _issue(result, f"标注 {a.get('id')} 的 bbox 非正或越界：{bbox}")
        if x + w > img["width"] + 1e-3 or y + h > img["height"] + 1e-3:
            _issue(result, f"标注 {a.get('id')} 的 bbox 超出图片尺寸 {img['width']}x{img['height']}")
        if a.get("area", 0) <= 0:
            _issue(result, f"标注 {a.get('id')} 的 area<=0")
        for ring in a.get("segmentation") or []:
            if len(ring) < 6 or len(ring) % 2:
                _issue(result, f"标注 {a.get('id')} 的分割环顶点数非法：{len(ring)}")

    missing = [e["file_name"] for e in images
               if not (out / (report.get("params") or {}).get("images_dir", "images")
                       / e["file_name"]).is_file()]
    if missing:
        _issue(result, f"{len(missing)} 个 image 文件不存在，例如 {missing[0]}")

    per_split = report.get("per_split") or {}
    total_split = 0
    for fold in ("train", "val", "test"):
        sub = _load_json(out / f"instances_{fold}.json", result)
        if sub is None:
            continue
        total_split += len(sub.get("images") or [])
        ids = {e["id"] for e in sub.get("images") or []}
        for e in images:
            if (e["id"] in ids) != ((e.get("mdva") or {}).get("split") == fold):
                _issue(result, f"图片 {e['file_name']} 的分折在总表与 {fold} 子表里不一致")
                break
    if total_split and total_split != len(images):
        _issue(result, f"三折图片数之和 {total_split} != 总数 {len(images)}")

    result["counts"].update({"written": len(annotations), "images": len(images),
                             "categories": len(cats)})
    if len(images) != report.get("n_images"):
        _issue(result, f"图片数 {len(images)} 与导出报告 {report.get('n_images')} 不符")
    if len(annotations) != report.get("n_annotations"):
        _issue(result, f"标注数 {len(annotations)} 与导出报告 {report.get('n_annotations')} 不符")
    result["checked"]["coco_keys"] = sorted(data.keys())


# ---------------------------------------------------------------------- YOLO

def _yolo(out: Path, report: dict, result: dict) -> None:
    text = _read_text(out / "data.yaml", result)
    if text is None:
        return
    nc = _yaml_int(text, "nc")
    names = re.findall(r"^  \d+: .+$", text, re.M)
    if nc is None:
        _issue(result, "data.yaml 里没有 nc")
    elif nc != len(names):
        _issue(result, f"data.yaml 声明 nc={nc}，但 names 有 {len(names)} 项")
    elif nc != report.get("n_categories"):
        _issue(result, f"nc={nc} 与导出报告的类别数 {report.get('n_categories')} 不符")

    task = report.get("params", {}).get("task", "detect")
    lines = 0
    images = 0
    unlabeled = 0
    for fold in ("train", "val", "test"):
        img_dir = out / "images" / fold
        lab_dir = out / "labels" / fold
        if not img_dir.is_dir():
            continue
        for img in sorted(img_dir.iterdir()):
            images += 1
            txt = lab_dir / (img.stem + ".txt")
            if not txt.is_file():
                unlabeled += 1
                continue
            for raw in txt.read_text(encoding="utf-8").splitlines():
                row = raw.split()
                if not row:
                    continue
                lines += 1
                if len(row) < 5:
                    _issue(result, f"{txt.name} 第{lines}行字段太少：{raw}")
                    continue
                try:
                    cls = int(row[0])
                    nums = [float(v) for v in row[1:]]
                except ValueError:
                    _issue(result, f"{txt.name} 有一行不是数字：{raw[:40]}")
                    continue
                if not 0 <= cls < (nc or 0):
                    _issue(result, f"{txt.name} 的类别号 {cls} 超出 [0, {nc})")
                if any(v < -1e-6 or v > 1 + 1e-6 for v in nums):
                    _issue(result, f"{txt.name} 存在未归一化或越界的坐标：{nums[:4]}")
                if task == "detect":
                    if len(nums) != 4:
                        _issue(result, f"detect 标签应为 5 个数，{txt.name} 有 {len(nums) + 1} 个")
                    elif not (0 < nums[2] <= 1 and 0 < nums[3] <= 1):
                        _issue(result, f"{txt.name} 的宽高为非正：{nums[2:]}")
                elif (len(nums) - 2) % 2 or len(nums) < 6:
                    _issue(result, f"segment 标签顶点数非法：{txt.name} 有 {(len(nums)) // 2} 个点")

    if images != report.get("n_images"):
        _issue(result, f"图片数 {images} 与导出报告 {report.get('n_images')} 不符")
    if lines != report.get("n_annotations"):
        _issue(result, f"标签行数 {lines} 与导出报告 {report.get('n_annotations')} 不符")
    result["counts"].update({"written": lines, "images": images, "background": unlabeled})


# ---------------------------------------------------------------------- VOC

def _voc(out: Path, report: dict, result: dict) -> None:
    ann_dir = out / "Annotations"
    img_dir = out / "JPEGImages"
    if not ann_dir.is_dir():
        _issue(result, "缺少 Annotations 目录")
        return
    objects = 0
    stems = []
    for xml in sorted(ann_dir.glob("*.xml")):
        stems.append(xml.stem)
        try:
            root = ET.parse(str(xml)).getroot()
        except ET.ParseError as exc:
            _issue(result, f"{xml.name} 不是合法 XML：{exc}")
            continue
        for key in ("filename", "size"):
            if root.find(key) is None:
                _issue(result, f"{xml.name} 缺少 <{key}>")
        size = root.find("size")
        w = int(size.findtext("width") or 0)
        h = int(size.findtext("height") or 0)
        name = root.findtext("filename") or ""
        if not (img_dir / name).is_file():
            _issue(result, f"{xml.name} 指向的图片不存在：JPEGImages/{name}")
        for obj in root.findall("object"):
            objects += 1
            bnd = obj.find("bndbox")
            if bnd is None:
                _issue(result, f"{xml.name} 有 object 缺 bndbox")
                continue
            x0, y0 = int(bnd.findtext("xmin") or 0), int(bnd.findtext("ymin") or 0)
            x1, y1 = int(bnd.findtext("xmax") or 0), int(bnd.findtext("ymax") or 0)
            if not (1 <= x0 <= x1 <= w and 1 <= y0 <= y1 <= h):
                _issue(result, f"{xml.name}/{obj.findtext('name')} 的 bndbox 越界或反序："
                               f"({x0},{y0},{x1},{y1}) vs {w}x{h}")
            poly = obj.findtext("polygon")
            if poly:
                pts = [p for p in poly.split(";") if p]
                if len(pts) < 3 or any(len(p.split(",")) != 2 for p in pts):
                    _issue(result, f"{xml.name} 的 polygon 扩展顶点非法")

    if objects != report.get("n_annotations"):
        _issue(result, f"VOC object 数 {objects} 与导出报告 {report.get('n_annotations')} 不符")
    if len(stems) != report.get("n_images"):
        _issue(result, f"XML 数量 {len(stems)} 与导出报告图片数 {report.get('n_images')} 不符")

    main = out / "ImageSets" / "Main"
    if not main.is_dir():
        _issue(result, "缺少 ImageSets/Main 切分清单")
        return
    seen: dict = {}
    for fold in ("train", "val", "test"):
        entries = _read_list(main / f"{fold}.txt")
        for stem in entries:
            if stem in seen:
                _issue(result, f"{stem} 同时出现在 {seen[stem]} 和 {fold} 折")
            seen[stem] = fold
            if stem not in stems:
                _issue(result, f"{fold}.txt 引用了不存在的标注 {stem}")
    if len(seen) != len(stems):
        _issue(result, f"切分清单覆盖 {len(seen)} 张，Annotations 有 {len(stems)} 张")
    result["counts"].update({"written": objects, "images": len(stems)})


# ---------------------------------------------------------------------- 多模态

def _multimodal(out: Path, report: dict, result: dict) -> None:
    data = _load_json(out / "mdva_dataset.json", result)
    if data is None:
        return
    for key in ("schema", "dataset", "version", "categories", "samples", "timebase"):
        if key not in data:
            _issue(result, f"mdva_dataset.json 缺少顶层字段 {key}")
    samples = data.get("samples") or []
    rows = 0
    for s in samples:
        if s.get("modality") == "unknown":
            _issue(result, f"样本 {s.get('name')} 的数据项无法解析")
        if s.get("file_name") and not (out / "images" / s["file_name"]).is_file():
            _issue(result, f"样本 {s['name']} 的落盘图片不存在")
        if s.get("mask_file") and not (out / "masks" / s["mask_file"]).is_file():
            _issue(result, f"样本 {s['name']} 的掩膜不存在")
        for a in s.get("annotations") or []:
            rows += 1
            if a.get("geom") not in GEOMS:
                _issue(result, f"样本 {s['name']} 出现未知几何 {a.get('geom')}")
            coords = a.get("coords") or []
            if not coords or len(coords) % 2:
                _issue(result, f"样本 {s['name']} 的 {a.get('geom')} 坐标个数非法：{coords}")
            if a.get("source") not in ("manual", "visual_prompt", "preannotate",
                                       "active_learning", "import"):
                _issue(result, f"样本 {s['name']} 的标注来源未知：{a.get('source')}")
            if a.get("geom") in ("interval", "level") and s.get("t_ref") is None:
                _issue(result, f"样本 {s['name']} 有时间几何却缺 t_ref")
    index = out / "annotations.csv"
    if not index.is_file():
        _issue(result, "缺少 annotations.csv 扁平索引")
    elif len(index.read_text(encoding="utf-8-sig").splitlines()) - 1 != rows:
        _issue(result, f"annotations.csv 行数与 JSON 标注数不符（{rows}）")
    result["counts"].update({"written": rows, "images": sum(1 for s in samples if s.get("file_name")),
                             "samples": len(samples),
                             "categories": len(data.get("categories") or [])})
    if rows != report.get("n_annotations"):
        _issue(result, f"标注数 {rows} 与导出报告 {report.get('n_annotations')} 不符")


# ---------------------------------------------------------------------- 工具

def _load_json(path: Path, result: dict):
    if not path.is_file():
        _issue(result, f"缺少 {path.name}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _issue(result, f"{path.name} 不是合法 JSON：{exc}")
        return None


def _read_text(path: Path, result: dict):
    if not path.is_file():
        _issue(result, f"缺少 {path.name}")
        return None
    return path.read_text(encoding="utf-8")


def _read_list(path: Path) -> list:
    if not path.is_file():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _yaml_int(text: str, key: str):
    m = re.search(rf"^{key}:\s*(\d+)\s*$", text, re.M)
    return int(m.group(1)) if m else None


def _issue(result: dict, message: str) -> None:
    """问题超过上限只截断显示，计数仍然累加——否则"刚好 50 条"会假装很干净。"""
    result["n_issues"] = result.get("n_issues", 0) + 1
    if len(result["issues"]) < MAX_ISSUES:
        result["issues"].append(message)


def summarize(result: dict) -> str:
    """给界面和 CLI 的一行结论。"""
    head = f"[{'OK' if result['ok'] else 'FAIL'}] {result['format']} @ {result['version']}"
    counts = {k: v for k, v in result["counts"].items() if isinstance(v, int)}
    tail = "；问题 " + "；".join(result["issues"][:5]) if result["issues"] else ""
    return f"{head} {counts}{tail}"


__all__ = ["validate", "summarize", "MAX_ISSUES"]
