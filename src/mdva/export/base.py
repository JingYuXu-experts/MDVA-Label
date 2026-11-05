"""导出层底座：把"版本 + 标注"变成一个与格式无关的中继结构。

四种训练格式（COCO / YOLO / VOC / 本项目多模态 JSON）都只读 `Dataset`，
不直接碰数据库，也不碰 L1 加载器。这样新增一个导出格式不需要动别人，
而坐标换算、切片展开、掩膜落盘这些容易出错的活只有一份实现。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from ..core.model import Modality, Slice
from ..core.registry import Registry, make_register_func
from ..annotate.model import Ann

EXPORTERS = Registry("导出器")
register = make_register_func(EXPORTERS)

SPATIAL = (Modality.IMAGE.value, Modality.CUBE.value, Modality.VIDEO.value)
TIME_BASED = (Modality.SERIES.value, Modality.EVENT.value)
NATIVE_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff")
NO_GROUP = "(全部)"

_SAFE = re.compile(r"[^0-9A-Za-z_.-]+")


def safe_part(text) -> str:
    """把路径片段变成可直接落盘的文件名，跨目录同名不会互相覆盖。"""
    return _SAFE.sub("_", str(text).strip()).strip("._") or "x"


def group_key(uri) -> str:
    """同一批切片的公共前缀：scene_0007 -> scene，slice_0012 -> slice。

    分层切分按这个键分组，否则断层序列的相邻两片会一张进训练集一张进验证集，
    测出来的精度是假的。
    """
    stem = Path(uri).stem
    return re.sub(r"[_-]?\d+$", "", stem).lower() or stem.lower()


@dataclass
class Sample:
    """一个导出数据集里的"一张图"：数据项 + 它的那一片 + 落在该片上的标注。"""

    uri: str
    name: str
    modality: str = "image"
    width: int = 0
    height: int = 0
    slice_index: int = 0
    t_ref: Optional[float] = None
    t_end: Optional[float] = None
    mask_uri: Optional[str] = None
    item: Optional[object] = None
    anns: list = field(default_factory=list)
    ann_meta: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def is_spatial(self) -> bool:
        return self.modality in SPATIAL and self.width > 0 and self.height > 0

    @property
    def is_mask(self) -> bool:
        return bool(self.mask_uri)

    def size(self) -> tuple:
        return (self.width, self.height)

    def pairs(self):
        """(标注, 数据库行摘要) 成对给出，导出层要留的 source/state 都在这里。"""
        return list(zip(self.anns, self.ann_meta))

    def time_local(self, t: float) -> Optional[float]:
        if self.t_ref is None:
            return None
        return t - self.t_ref


@dataclass
class Dataset:
    """中继结构。`issues` 记录被丢弃的标注及原因，导出报告与校验都要能对上。"""

    dataset: dict
    version: dict
    samples: list = field(default_factory=list)
    categories: list = field(default_factory=list)
    labels: list = field(default_factory=dict)
    issues: list = field(default_factory=list)

    def category_ids(self) -> dict:
        return {name: i for i, name in enumerate(self.categories)}

    def spatial(self) -> list:
        return [s for s in self.samples if s.is_spatial]

    def masks(self) -> list:
        """带配对掩膜的空间样本——分割导出要用它，而不是把掩膜当图片本身。"""
        return [s for s in self.samples if s.is_mask]

    def time_samples(self) -> list:
        return [s for s in self.samples if not s.is_spatial]

    def stats(self) -> dict:
        by_geom: dict = {}
        by_source: dict = {}
        for s in self.samples:
            for a in s.anns:
                by_geom[a.geom] = by_geom.get(a.geom, 0) + 1
                by_source[a.source] = by_source.get(a.source, 0) + 1
        return {"n_samples": len(self.samples), "n_spatial": len(self.spatial()),
                "n_masks": len(self.masks()), "n_time": len(self.time_samples()),
                "n_annotations": sum(len(s.anns) for s in self.samples),
                "by_geom": by_geom, "by_source": by_source,
                "categories": list(self.categories), "n_issues": len(self.issues)}


def _size_of(item) -> tuple:
    """(width, height)：图像类从头部 shape 取，时序/事件类没有像素尺寸。"""
    if item.modality not in SPATIAL:
        return (0, 0)
    shape = tuple(item.shape or ())
    if len(shape) >= 2:
        return (int(shape[-1]), int(shape[-2]))
    try:
        from ..render.image import pick_slice

        arr = pick_slice(item, Slice.at_index(0))
    except Exception:
        return (0, 0)
    arr = getattr(arr, "shape", ())
    return (int(arr[-1]), int(arr[-2])) if len(arr) >= 2 else (0, 0)


def _mask_for(item, root: Path) -> Optional[str]:
    """分割训练对要的是"图 + 类别编号掩膜"，掩膜按四种常见摆放方式去找。"""
    hint = item.meta.get("mask") or item.meta.get("mask_uri")
    if hint:
        p = Path(hint)
        p = p if p.is_absolute() else (root / p)
        return str(p) if p.is_file() else None
    path = Path(item.uri)
    for cand in (path.with_name(path.stem + "_mask.png"), path.with_name(path.stem + ".mask.png"),
                 path.parent / "mask_gt" / (path.stem + ".png"),
                 path.parent.parent / "mask_gt" / (path.stem + ".png"),
                 path.parent.parent / "masks" / (path.stem + ".png")):
        if cand.is_file():
            return str(cand)
    return None


def _resolve(uri: str, root: Optional[Path]):
    p = Path(uri)
    if not p.is_absolute() and root:
        p = root / p
    return p


def load_dataset(store, version_id: str, states: Optional[Sequence[str]] = None,
                 min_confidence: Optional[float] = None, only_labels: Optional[Sequence[str]] = None,
                 modalities: Optional[Sequence[str]] = None, root=None) -> Dataset:
    """读出一个版本的全部标注并按 (数据项, 切片) 展开成样本。

    立方体和帧序列上，标注是钉在某一层的：把同一项的不同切片当成不同样本，
    导出结果才和画布上看到的一致。
    """
    version = store.get_version(version_id)
    if version is None:
        raise KeyError(f"未知版本 {version_id}")
    dataset = store.get_dataset(version["dataset_id"]) or {}
    base = Path(root or dataset.get("root") or ".")

    rows = store.list_annotations(version_id)
    picked = []
    for row in rows:
        ann = row["ann"]
        if states and row["state"] not in states:
            continue
        if min_confidence is not None and ann.confidence < min_confidence:
            continue
        if only_labels and ann.label not in only_labels:
            continue
        picked.append((row["item_uri"], ann, row))

    ds = Dataset(dataset=dataset, version=version, labels=store.list_labels(dataset.get("id", "")))
    cache: dict = {}
    grouped: dict = {}
    for uri, ann, row in picked:
        key = (uri, ann.slice if ann.slice is not None else 0)
        grouped.setdefault(key, []).append((ann, row))

    for (uri, index), pairs in sorted(grouped.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        item = cache.get(uri, _MISS)
        if item is _MISS:
            item = _load_item(_resolve(uri, base), ds.issues, uri)
            cache[uri] = item
        pairs = sorted(pairs, key=lambda p: (p[0].geom, p[0].label, json.dumps(p[0].coords),
                                             p[1]["id"]))
        anns = [p[0] for p in pairs]
        metas = [{"id": p[1]["id"], "state": p[1]["state"], "role": p[1]["role"],
                  "ts": p[1]["ts"]} for p in pairs]
        states_ = sorted({m["state"] for m in metas})
        if item is None:
            width = height = 0
            modality = "unknown"
            t_ref = t_end = None
            meta = {}
        else:
            modality = item.modality
            width, height = _size_of(item)
            t_ref, t_end = item.time_span if item.t_ref is not None else (None, None)
            meta = dict(item.meta or {})
            meta.update({"title": item.title, "dtype": item.dtype, "n_views": item.n_views})
        if modality == "unknown":
            continue
        if modalities and modality not in modalities:
            continue
        name = _sample_name(uri, index, modality)
        sample = Sample(uri=str(uri), name=name, modality=modality, width=width, height=height,
                        slice_index=int(index), t_ref=t_ref, t_end=t_end, anns=anns,
                        ann_meta=metas, item=item, meta={**meta, "states": states_})
        if item is not None and modality in SPATIAL and width:
            sample.mask_uri = _mask_for(item, base)
        ds.samples.append(sample)

    ds.categories = _categories(ds)
    return ds


class _Missing:
    pass


_MISS = _Missing()


def _load_item(path, issues: list, uri: str):
    from .. import io as MDIO

    try:
        if not Path(path).exists():
            raise FileNotFoundError(path)
        return MDIO.load_any(str(path))
    except Exception as exc:
        issues.append({"kind": "unresolved_item", "uri": str(uri), "error": str(exc)})
        return None


def _sample_name(uri: str, index: int, modality: str) -> str:
    path = Path(uri)
    stem = safe_part(path.stem)
    parts = [safe_part(p) for p in path.parent.parts[-2:]]
    prefix = "_".join(parts + [stem])
    if modality == Modality.IMAGE.value and index == 0:
        return prefix
    return f"{prefix}_s{int(index):04d}"


def _categories(ds: Dataset) -> list:
    """标签组表优先（保留界面顺序与快捷键），再补标注里出现过但没登记的标签。"""
    out = []
    for lab in ds.labels:
        name = lab.get("name")
        if name and name not in out:
            out.append(name)
    seen = set(out)
    extra = {a.label for s in ds.samples for a in s.anns}
    for name in sorted(x for x in extra if x not in seen):
        out.append(name or "(未分类)")
    return out or ["(未分类)"]


def category_of(ds: Dataset, label: str) -> int:
    if not label:
        label = "(未分类)"
    ids = ds.category_ids()
    return ids.get(label, ids.get("(未分类)", 0))


# ---------------------------------------------------------------------- 坐标换算

def clamp_box(ann: Ann, width: int, height: int, issues: list, sample: Sample) -> Optional[tuple]:
    """像素框 -> [x, y, w, h]，越界部分裁掉；裁完没面积的框要报告而不是静默丢。"""
    x0, y0, x1, y1 = ann.coords
    cx0, cy0 = max(0.0, min(x0, width)), max(0.0, min(y0, height))
    cx1, cy1 = max(0.0, min(x1, width)), max(0.0, min(y1, height))
    if cx1 - cx0 < 1.0 or cy1 - cy0 < 1.0:
        issues.append({"kind": "box_out_of_bounds", "uri": sample.uri, "slice": sample.slice_index,
                       "coords": list(ann.coords), "size": [width, height]})
        return None
    return (cx0, cy0, cx1 - cx0, cy1 - cy0)


def polygon_rings(ann: Ann, width: int, height: int) -> list:
    pts = list(zip(ann.coords[0::2], ann.coords[1::2]))
    clamped = [(min(max(x, 0.0), width), min(max(y, 0.0), height)) for x, y in pts]
    return [[v for xy in clamped for v in xy]]


def normalized(values: Sequence[float], width: int, height: int) -> list:
    out = []
    for i, v in enumerate(values):
        out.append(min(max(v / (width if i % 2 == 0 else height), 0.0), 1.0))
    return out


# ---------------------------------------------------------------------- 图像落盘

def write_sample_image(sample: Sample, dst_dir: Path) -> dict:
    """把样本落盘成训练框架能直接读的文件。

    普通位图整张复制（原件不动、字节一致），科学格式与切片按原始尺寸出图 ——
    尺寸不变，标注坐标才能 1:1 搬过去，不需要缩放反算。
    """
    from PIL import Image

    dst_dir.mkdir(parents=True, exist_ok=True)
    item = sample.item
    if item is None:
        raise ValueError(f"样本 {sample.uri} 未绑定数据项，无法出图")

    src = Path(sample.uri)
    if (sample.modality == Modality.IMAGE.value and sample.slice_index == 0
            and src.suffix.lower() in NATIVE_IMAGE_SUFFIXES):
        out = dst_dir / f"{sample.name}{src.suffix.lower()}"
        shutil.copy2(str(src), str(out))
        return {"file_name": out.name, "width": sample.width, "height": sample.height,
                "mode": "copy"}

    from ..render.image import pick_slice, render_array

    arr = pick_slice(item, Slice.at_index(sample.slice_index))
    style = dict(sample.meta.get("style") or {})
    rgb, _info = render_array(np.asarray(arr), (sample.width, sample.height),
                              keep_aspect=False, **style)
    out = dst_dir / f"{sample.name}.png"
    Image.fromarray(rgb).save(str(out))
    return {"file_name": out.name, "width": sample.width, "height": sample.height, "mode": "render"}


def write_sample_mask(sample: Sample, dst_dir: Path) -> Optional[dict]:
    """类别编号掩膜整张复制：编号是标签不是强度，重采样会把三类糊成三种灰。"""
    if not sample.mask_uri:
        return None
    from PIL import Image

    dst_dir.mkdir(parents=True, exist_ok=True)
    src = Path(sample.mask_uri)
    out = dst_dir / f"{sample.name}_mask.png"
    shutil.copy2(str(src), str(out))
    w, h = Image.open(str(out)).size
    return {"file_name": out.name, "width": int(w), "height": int(h),
            "mode": "mask", "source": str(src)}


def write_report(out_dir: Path, ds: Dataset, fmt: str, params: dict, body: dict) -> dict:
    """每个导出目录都留一份自描述报告，校验器拿它和数据库对账。"""
    report = {
        "format": fmt,
        "dataset": ds.dataset.get("name"), "dataset_id": ds.dataset.get("id"),
        "type": ds.dataset.get("type"), "root": ds.dataset.get("root"),
        "version": ds.version.get("id"), "parent": ds.version.get("parent_id"),
        "op": ds.version.get("op"), "params": params,
        "counts": ds.stats(), "issues": ds.issues, **body,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "dataset_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def read_report(export_dir) -> dict:
    path = Path(export_dir) / "dataset_report.json"
    if not path.is_file():
        raise FileNotFoundError(f"{path} 不存在：这个目录不是本工具导出的数据集")
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = ["EXPORTERS", "register", "Dataset", "Sample", "SPATIAL", "TIME_BASED",
           "load_dataset", "write_sample_image", "write_sample_mask", "write_report",
           "read_report",
           "clamp_box", "polygon_rings", "normalized", "category_of", "group_key",
           "safe_part", "NO_GROUP"]
