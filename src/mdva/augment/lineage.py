"""增强谱系：配方 → 逐条计划 → 落盘入库 → 同 seed 重放。

谱系记的是**抽出来的具体参数**，不是参数范围：范围以后会被改，抽出来的数才是这张图
的成因。`replay()` 用版本里存的配方和 seed 重新抽一遍，逐条和落盘谱系比对——
"同 seed 重放必须给同一条链"，否则论文里"用 3 倍增强数据训练"这句话没法核验。

一个诚实的限制：库里以标注为条目索引，所以**没有标注的条目增强后只是文件产物**
（给无监督/自监督用），任务报告里登记了它们，但版本查不到这些条目。
"""

from __future__ import annotations

import csv
import json
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from ..clean.ops import map_geometry
from ..clean.task import inherit, load_items, producing_task
from ..core.model import Modality, Slice, TimeSeries
from . import ops as A

DEFAULT_ROOT = Path("out")


@dataclass
class Recipe:
    """一份可重放的增强配方。`ops` 用 `augment.ops.build` 收的同一套 spec 格式。"""

    seed: int = 0
    factor: int = 1
    ops: list = field(default_factory=list)
    limit: Optional[int] = None
    index: int = 0

    def to_json(self) -> dict:
        return {"seed": int(self.seed), "factor": int(self.factor),
                "ops": [dict(o) for o in self.ops], "limit": self.limit,
                "index": int(self.index)}

    @classmethod
    def from_obj(cls, obj) -> "Recipe":
        if isinstance(obj, Recipe):
            return obj
        if isinstance(obj, (list, tuple)):
            obj = {"ops": list(obj)}
        if not isinstance(obj, dict):
            raise TypeError(f"无法解析增强配方: {obj!r}")
        known = set(cls.__dataclass_fields__)
        unknown = set(obj) - known
        if unknown:
            print(f"[warn] 增强配方里的未知字段将被忽略: {sorted(unknown)}")
        payload = {k: v for k, v in obj.items() if k in known}
        payload["ops"] = [{"name": o} if isinstance(o, str) else dict(o)
                          for o in payload.get("ops") or []]
        if not payload["ops"]:
            raise ValueError("增强配方至少要有一个算子")
        return cls(**payload)


@dataclass
class Job:
    """一个待产出的增强副本：源条目 + 副本号 + 抽好的参数。"""

    uri: str
    rep: int
    specs: list = field(default_factory=list)

    def to_json(self) -> dict:
        return {"uri": self.uri, "rep": self.rep,
                "specs": [s.to_json() for s in self.specs]}

    @classmethod
    def from_json(cls, obj: dict) -> "Job":
        return cls(str(obj["uri"]), int(obj["rep"]),
                   [A.Spec.from_json(s) for s in obj.get("specs") or []])


def plan(items: Sequence, recipe: Recipe) -> list:
    """确定性排产：按 uri 排序，用 (seed, 序号, 副本号) 派生随机流。

    排序而不是沿用传入顺序，是因为目录扫描和数据库返回顺序都可能变；
    SeedSequence 的熵必须是非负整数，所以 seed 先取模。
    """
    operators = A.build(recipe.ops)
    usable = [i for i in items if any(o.handles(i.modality) for o in operators)]
    if len(usable) != len(items):
        names = "/".join(o.name for o in operators)
        print(f"[info] {len(items) - len(usable)} 个条目没有算子能处理，跳过（配方 {names}）")
    ordered = sorted(usable, key=lambda i: str(i.uri))
    if recipe.limit:
        ordered = ordered[: int(recipe.limit)]
    entropy = int(recipe.seed or 0) & 0x7FFF_FFFF
    factor = max(1, int(recipe.factor))
    jobs = []
    for i, item in enumerate(ordered):
        for rep in range(factor):
            rng = np.random.default_rng(np.random.SeedSequence([entropy, i, rep]))
            jobs.append(Job(str(item.uri), rep, A.draw(operators, rng)))
    return jobs


# ---------------------------------------------------------------------- 落盘

@lru_cache(maxsize=32)
def _source_calib(source_uri: str) -> dict:
    """条目旁的标定表按路径缓存：一次增强里每个离散通道都要查一次，别反复读盘。"""
    from ..io import _auto_calibration
    from ..io.series import Calibration

    found = _auto_calibration(Path(source_uri)) if source_uri else None
    return {} if found is None else dict(Calibration.load(found).table)


def _labels(source_uri: str, channel: str) -> dict:
    """离散通道的码值含义只存在于源侧标定表里，新侧车必须原样继承，否则枚举语义丢失。"""
    spec = _source_calib(source_uri).get(channel) or {}
    return {"values": spec["values"]} if spec.get("values") else {}


def write_series_csv(path, ts: TimeSeries, source_uri: str = "") -> dict:
    """写增强后的遥测 CSV + 标定侧车。

    侧车里模拟通道改成 scale=1 / offset=0：内存里已经是标定后的工程值，
    再挂原表会让加载器第二次乘 scale——同一条曲线被标定两遍，数值直接错。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    discrete = [bool(d) for d in ts.discrete]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["utc"] + [str(c) for c in ts.channels])
        for i in range(ts.t.size):
            row = [f"{ts.t[i]:.3f}"]
            for j in range(ts.values.shape[1]):
                v = float(ts.values[i, j])
                if not np.isfinite(v):
                    row.append("")
                elif discrete[j]:
                    row.append(str(int(round(v))))
                else:
                    row.append(f"{v:.6f}")
            writer.writerow(row)

    table = {}
    for j, name in enumerate(ts.channels):
        key = str(name)
        if discrete[j]:
            table[key] = {"type": "discrete", "unit": ts.units[j],
                          **_labels(source_uri, key)}
        else:
            table[key] = {"type": "analog", "scale": 1.0, "offset": 0.0, "unit": ts.units[j]}
    sidecar = path.with_name(path.stem + ".calib.json")
    sidecar.write_text(json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"file": str(path), "sidecar": str(sidecar), "bytes": path.stat().st_size,
            "n_samples": int(ts.t.size), "n_channels": int(ts.values.shape[1])}


def _execute(pos: int, job: Job, item, out_root: Path, recipe: Recipe,
             masks: Optional[dict] = None) -> dict:
    """跑一个副本：算像素/数值 → 落盘 → 记录谱系。"""
    from ..render.image import pick_slice, save_array

    stem = f"{pos:04d}_{Path(job.uri).stem}_r{job.rep}"
    sl = Slice.at_index(int(recipe.index))
    record = {"source": job.uri, "rep": job.rep, "modality": item.modality,
              "specs": [s.to_json() for s in job.specs]}

    if item.modality == Modality.SERIES.value:
        ts = item.load()
        augmented, chain = A.apply_series(ts, job.specs)
        info = write_series_csv(out_root / "series" / f"{stem}.csv", augmented,
                                source_uri=str(job.uri))
        return {**record, "written": str(Path(info["file"]).resolve()),
                "bytes": info["bytes"], "chain": [{"op": n, "params": p} for n, p in chain],
                "source_shape": list(ts.values.shape), "n_samples": info["n_samples"],
                "aux": {"sidecar": {"written": str(Path(info["sidecar"]).resolve()),
                                    "bytes": Path(info["sidecar"]).stat().st_size,
                                    "source": str(job.uri)}}}

    arr = np.asarray(pick_slice(item, sl))
    mask_path = (masks or {}).get(str(job.uri)) or _discover_mask(item)
    labels = np.asarray(_read_mask(mask_path)) if mask_path else None
    out, out_mask, chain = A.apply_image(arr, labels, job.specs, item.modality)
    info = save_array(out_root / "img" / f"{stem}.png", out)
    record.update({"written": str(Path(info["file"]).resolve()), "bytes": info["bytes"],
                   "chain": [{"op": n, "params": p} for n, p in chain],
                   "source_shape": list(arr.shape),
                   "width": info["width"], "height": info["height"]})
    if out_mask is not None:
        minfo = save_array(out_root / "mask" / f"{stem}_mask.png", out_mask, stretch=False)
        record["aux"] = {"mask": {"written": str(Path(minfo["file"]).resolve()),
                                  "bytes": minfo["bytes"], "source": str(mask_path)}}
    return record


def _discover_mask(item) -> Optional[str]:
    """掩膜按导出层那套目录约定找（`_mask.png` 同级、`mask_gt/`、`masks/`）。"""
    from ..export.base import _mask_for

    return _mask_for(item, Path(str(item.uri)).parent)


def _read_mask(path):
    from PIL import Image

    return np.asarray(Image.open(str(path)))


# ---------------------------------------------------------------------- 执行

def run(store, version_id: str, recipe, out_dir=None, uris=None,
        masks: Optional[dict] = None, author: str = "") -> dict:
    """执行增强：产新版本、写新文件、把标注按链映射过去。原件与父版本一律不动。"""
    recipe = Recipe.from_obj(recipe)
    version = store.get_version(version_id)
    if version is None:
        raise KeyError(f"未知版本 {version_id}")
    if uris is None:
        uris = sorted(store.annotated_items(version_id))
    items = load_items(uris)
    jobs = plan(items, recipe)
    if not jobs:
        raise ValueError("配方里没有能处理这些条目的算子，不产出增强版本")

    operators = A.build(recipe.ops)
    new_version = store.put_version(version["dataset_id"], version_id, "augment", {
        "recipe": recipe.to_json(), "author": author,
    }, stats={"n_sources": len({j.uri for j in jobs}), "n_generated": len(jobs),
              "seed": int(recipe.seed), "factor": int(recipe.factor),
              "ops": [o.name for o in operators]})

    out_root = Path(out_dir or DEFAULT_ROOT) / "aug" / str(new_version)
    out_root.mkdir(parents=True, exist_ok=True)
    by_uri = {str(i.uri): i for i in items}
    written = []
    for pos, job in enumerate(jobs):
        written.append(_execute(pos, job, by_uri[job.uri], out_root, recipe, masks))

    n_inherited = _attach(store, version_id, new_version, written)
    aux = [a for r in written for a in (r.get("aux") or {}).values()]
    summary = {"n_sources": len({r["source"] for r in written}), "n_generated": len(written),
               "n_masks": sum(1 for r in written if (r.get("aux") or {}).get("mask")),
               "n_series": sum(1 for r in written if r["modality"] == Modality.SERIES.value),
               "n_files": len(written) + len(aux)}

    task = store.create_task("augment", version["dataset_id"], version_id, new_version,
                             {"recipe": recipe.to_json()})
    report = {"version": new_version, "parent": version_id, "recipe": recipe.to_json(),
              "out_dir": str(out_root), "written": written, "summary": summary,
              "n_inherited": n_inherited}
    (out_root / "lineage.json").write_text(
        json.dumps({"recipe": recipe.to_json(), "written": written},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    store.update_task(task, "done", report, version_out=new_version)
    return {**report, "task_id": task}


def _attach(store, from_version: str, to_version: str, written: Sequence[dict]) -> int:
    """原件标注全量继承（增强是加样本不是换样本），再给每个副本挂一份映射过几何的标注。"""
    inherit(store, from_version, to_version)
    copied = 0
    for rec in written:
        for row in store.list_annotations(from_version, rec["source"]):
            ann, _ = map_geometry(row["ann"], [(c["op"], c["params"]) for c in rec["chain"]],
                                  tuple(rec["source_shape"]))
            store.put_annotation(to_version, rec["written"], ann, role=row["role"],
                                 state=row["state"])
            copied += 1
    return copied


def read_lineage(store, version_id: str) -> list:
    """读回某个增强版本的逐条谱系（数据库是唯一真源，`lineage.json` 只是给人看的副本）。"""
    task = producing_task(store, version_id)
    return ((task or {}).get("report") or {}).get("written") or []


def replay(store, version_id: str, items=None) -> dict:
    """按版本里存的配方 + seed 重抽一遍参数，和落盘谱系逐条比。

    比的是"链"而不是重算像素：像素一致是 numpy/PIL 版本的事，
    链一致才是这次增强可不可复现的事。
    """
    version = store.get_version(version_id)
    if version is None:
        raise KeyError(f"未知版本 {version_id}")
    if version["op"] != "augment":
        raise ValueError(f"版本 {version_id} 是 {version['op']} 产出的，没有增强谱系可重放")
    recipe = Recipe.from_obj((version.get("params") or {}).get("recipe") or {})
    stored = read_lineage(store, version_id)
    if not stored:
        raise ValueError(f"版本 {version_id} 的增强谱系不在数据库里")

    if items is None:
        items = load_items(sorted({r["source"] for r in stored}))
    rebuilt = plan(items, recipe)
    by_key = {(r["source"], int(r["rep"])): r["specs"] for r in stored}
    seen, diff = set(), []
    for job in rebuilt:
        key = (job.uri, job.rep)
        seen.add(key)
        got = [s.to_json() for s in job.specs]
        want = by_key.get(key)
        if want is None:
            diff.append({"uri": job.uri, "rep": job.rep, "why": "重放多出了副本"})
        elif want != got:
            diff.append({"uri": job.uri, "rep": job.rep, "why": "参数不一致",
                         "stored": want, "replayed": got})
    for key in by_key:
        if key not in seen:
            diff.append({"uri": key[0], "rep": key[1], "why": "重放少了这条"})
    return {"identical": not diff, "recipe": recipe.to_json(), "n_jobs": len(rebuilt),
            "n_stored": len(stored), "diff": diff}


__all__ = ["DEFAULT_ROOT", "Job", "Recipe", "plan", "read_lineage", "replay", "run",
           "write_series_csv"]
