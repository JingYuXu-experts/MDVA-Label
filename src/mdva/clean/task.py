"""清洗任务编排：一次算子串 → 预览 / 执行 / 回退。

预览和执行共用同一个 `evaluate()`，界面上滑杆旁边的"将剔除 N 条"就是这里算出来的，
点执行之后剔除的必然是同一批。执行只产新版本：原件、旧版本、被剔除的条目本身都不删。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np

from ..core.model import DataItem, Slice
from . import ops as OPS


@dataclass
class Result:
    kept: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    transforms: list = field(default_factory=list)
    decisions: list = field(default_factory=list)
    scores: dict = field(default_factory=dict)
    per_op: dict = field(default_factory=dict)
    groups: list = field(default_factory=list)

    def summary(self) -> dict:
        return {"n_items": len(self.kept) + len(self.dropped), "n_kept": len(self.kept),
                "n_dropped": len(self.dropped), "n_transforms": len(self.transforms),
                "per_op": self.per_op, "groups": len(self.groups)}


def build(specs: Sequence[dict], calibration=None) -> list:
    """把 [{name, params}] 变成算子实例。

    content_filter 要带提示（dict 或 Prompt 都收），series_clean 要带标定表，
    这两个东西不属于"参数面板"，所以从 params 里摘出来单独传。
    """
    from ..backends.base import Prompt

    out = []
    for spec in specs or []:
        name = spec.get("name") or spec.get("op")
        if not name:
            raise ValueError(f"清洗算子缺少 name：{spec}")
        if name not in OPS.CLEAN_OPS:
            raise KeyError(f"未知清洗算子 {name}，可用 {OPS.CLEAN_OPS.names()}")
        params = dict(spec.get("params") or {})
        for key in spec:
            if key not in ("name", "op", "params"):
                params[key] = spec[key]
        kwargs = {}
        if name == "content_filter":
            kwargs["prompts"] = [Prompt.from_json(p) if isinstance(p, dict) else p
                                 for p in params.pop("prompts", [])]
        if name == "series_clean" and calibration is not None:
            kwargs["calibration"] = calibration
        out.append(OPS.CLEAN_OPS.get(name).create(**params, **kwargs))
    return out


def load_items(uris: Iterable) -> list:
    from .. import io as MDIO

    items = []
    for uri in uris:
        if isinstance(uri, DataItem):
            items.append(uri)
            continue
        try:
            items.append(MDIO.load_any(str(uri)))
        except Exception as exc:
            raise ValueError(f"数据项读不出来：{uri}（{exc}）") from None
    return items


def evaluate(items: Sequence[DataItem], specs: Sequence[dict], calibration=None) -> Result:
    """跑一遍所有算子。dedup 的"保留最清晰者"要清晰度分数，这里统一算一次，不重复读图。"""
    result = Result()
    operators = build(specs, calibration)
    need_sharpness = any(getattr(o, "name", "") == "dedup" and
                         o.params.get("keep") == "sharpest" for o in operators)
    scores: dict = {}
    if need_sharpness:
        from .ops import Blur

        blur = Blur()
        scores = {str(i.uri): blur.sharpness(i) for i in items if _handles(blur, i)}

    for op in operators:
        subset = [i for i in items if _handles(op, i)]
        if getattr(op, "name", "") == "dedup":
            decisions = op.evaluate(subset, scores)
            result.groups = op.groups(subset, scores)
        elif getattr(op, "is_transform", False):
            decisions = [OPS.Decision(str(i.uri), op.name, keep=True,
                                       transform=op.name, params=dict(op.params))
                         for i in subset]
        else:
            decisions = op.evaluate(subset, scores)
        result.decisions.extend(decisions)
        result.per_op[op.name] = {"n": len(subset),
                                  "dropped": sum(1 for d in decisions if not d.keep),
                                  "transformed": sum(1 for d in decisions if d.transform)}

    dropped: dict = {}
    for d in result.decisions:
        if not d.keep:
            dropped.setdefault(d.uri, d)
    transforms: dict = {}
    for d in result.decisions:
        if d.transform:
            transforms.setdefault(d.uri, []).append(d)
    result.scores = scores
    for item in items:
        uri = str(item.uri)
        if uri in dropped:
            result.dropped.append({"uri": uri, "op": dropped[uri].op,
                                   "reason": dropped[uri].reason,
                                   "score": dropped[uri].score})
        else:
            result.kept.append(uri)
            if uri in transforms:
                for d in transforms[uri]:
                    result.transforms.append({"uri": uri, "op": d.op, "params": d.params})
    return result


def _handles(op, item) -> bool:
    modalities = getattr(op, "modalities", None)
    return True if modalities is None else item.modality in modalities


def preview(items, specs, calibration=None) -> dict:
    """只算不改：给界面滑杆用。与 apply() 走同一条 evaluate()。

    M4-A 起附带逐条命中清单 dropped（与 apply 报告同一条代码路径、同一形状），
    清洗视图的"命中条目缩略列表"预览阶段即可渲染，不需要先执行。
    """
    result = evaluate(items, specs, calibration)
    out = result.summary()
    out["dropped"] = result.dropped
    return out


# ---------------------------------------------------------------------- 执行

def chain_of(result: Result) -> dict:
    """uri -> [(op, params)]，保持 evaluate() 里的算子先后顺序。"""
    chain: dict = {}
    for row in result.transforms:
        chain.setdefault(row["uri"], []).append((row["op"], dict(row["params"])))
    return chain


def write_transforms(result: Result, out_dir, version_id: str, items=None) -> dict:
    """把每个条目的变换链合成一次执行，写成一张新图；原件只读，绝不覆盖。

    返回 {源 uri: 记录}，记录里带落盘路径、字节数、变换前的形状和链上的参数——
    一键回退只删这份记录登记过的文件，标注映射只按登记前的形状换算坐标。
    """
    from ..render.image import pick_slice, save_array

    out = Path(out_dir) / "clean" / str(version_id)
    out.mkdir(parents=True, exist_ok=True)
    by_uri = {str(i.uri): i for i in (items or [])}
    records: dict = {}
    for pos, (uri, chain) in enumerate(chain_of(result).items()):
        item = by_uri.get(uri)
        if item is None:
            continue
        arr = np.asarray(pick_slice(item, Slice.at_index(0)))
        source_shape = list(arr.shape)
        for name, params in chain:
            arr = OPS.CLEAN_OPS.get(name).create(**params).apply(arr)
        info = save_array(out / f"{pos:04d}_{Path(uri).stem}.png", arr)
        records[uri] = {"written": str(Path(info["file"]).resolve()), "source": uri,
                        "chain": [{"op": n, "params": p} for n, p in chain],
                        "source_shape": source_shape, "bytes": info["bytes"]}
    OPS.write_manifest(out / "written.json", list(records.values()))
    return records


def inherit(store, from_version: str, to_version: str, keep=None,
            records: Optional[dict] = None, role: str = "") -> int:
    """把一个版本的标注带到另一个版本：`keep` 之外的不带，被变换的按链改几何并挂到新文件上。

    不继承的话，清洗完的新版本是空的，导出会得到 0 条样本——那等于把人工标注洗没了。
    审核状态与提交人原样带过去：清洗不该把已经 submit 的标注打回草稿。
    """
    records = records or {}
    allowed = set(keep) if keep is not None else None
    copied = 0
    for row in store.list_annotations(from_version):
        uri = row["item_uri"]
        # D3：库里是相对 uri，而 keep/records 来自磁盘侧（绝对路径）——比对前先还原，
        # 否则整版继承会静默失配，人工标注被"洗没"。
        abs_uri = store.to_abs(uri)
        if allowed is not None and uri not in allowed and abs_uri not in allowed:
            continue
        rec = records.get(abs_uri) or records.get(uri)
        ann = row["ann"]
        if rec:
            ann, _ = OPS.map_geometry(ann, [(c["op"], c["params"]) for c in rec["chain"]],
                                      tuple(rec["source_shape"]))
        store.put_annotation(to_version, rec["written"] if rec else uri, ann,
                             role=role or row["role"], state=row["state"])
        copied += 1
    return copied


def apply(store, version_id: str, specs: Sequence[dict], uris=None, out_dir=None,
          calibration=None, write_transformed: bool = False, author: str = "") -> dict:
    """执行清洗并产新版本。`uris` 缺省用该版本里已标注涉及的全部数据项。

    变换只有落盘才改变条目 uri（`write_transformed=True` 且给了 `out_dir`）；
    不开落盘时变换仅记入 stats，标注仍挂原图——预览和"真的动手"共用 `evaluate()`。
    """
    version = store.get_version(version_id)
    if version is None:
        raise KeyError(f"未知版本 {version_id}")
    if uris is None:
        uris = sorted(store.annotated_items(version_id))
    items = load_items(uris)
    result = evaluate(items, specs, calibration)
    out_dir = Path(out_dir) if out_dir else None

    new_version = store.put_version(version["dataset_id"], version_id, "clean", {
        "ops": [{"name": name, "params": params} for name, params in _op_params(specs)],
        "author": author,
    }, stats={"n_items": len(items), "n_kept": len(result.kept),
              "n_dropped": len(result.dropped), "dropped": result.dropped,
              "per_op": result.per_op, "n_transforms": len(result.transforms)})

    written: dict = {}
    if write_transformed and result.transforms and out_dir:
        written = write_transforms(result, out_dir, new_version, items)

    # 只清洗了子集时，子集之外条目的标注必须原样带过去——清洗不该顺手删掉没参与判定的标签
    cleaned = {str(i.uri) for i in items}
    keep = list(result.kept) + [u for u in store.annotated_items(version_id) if u not in cleaned]
    n_inherited = inherit(store, version_id, new_version, keep, written)
    transformed = [{"source": r["source"], "written": r["written"],
                    "chain": r["chain"]} for r in written.values()]

    task = store.create_task("clean", version["dataset_id"], version_id, new_version,
                             {"ops": [s.get("name") for s in specs]})
    report = {"version": new_version, "parent": version_id, "summary": result.summary(),
              "dropped": result.dropped, "kept": result.kept,
              "groups": [{"keep": g[0], "drop": g[1:]} for g in result.groups],
              "transforms": result.transforms, "written": list(written.values()),
              "out_dir": str(out_dir) if out_dir else "", "n_inherited": n_inherited}
    store.update_task(task, "done", report, version_out=new_version)
    return {**report, "transformed": transformed, "task_id": task,
            "decisions": [d.to_json() for d in result.decisions]}


def _op_params(specs: Sequence[dict]) -> list:
    out = []
    for spec in specs or []:
        name = spec.get("name") or spec.get("op")
        out.append((name, dict(spec.get("params") or {})))
    return out


def dropped_of(store, version_id: str) -> list:
    """从版本 stats 里读回被剔除清单——查"这一版为什么少了 3 张"不用重跑算子。"""
    version = store.get_version(version_id) or {}
    return (version.get("stats") or {}).get("dropped") or []


def producing_task(store, version_id: str) -> Optional[dict]:
    """找到"产出这个版本"的那条任务——回退要靠它登记过的写盘清单。"""
    for task in store.list_tasks():
        if task["version_out"] == version_id:
            return task
    return None


def _deletable(record: dict) -> list:
    """一条写盘记录覆盖的所有文件：主产物 + aux（掩膜、标定侧车）。

    登记本身（written.json / lineage.json）不在这里，也不删——那是审计线索不是数据产物。
    """
    out = [(record["written"], int(record.get("bytes", -1)))]
    for aux in (record.get("aux") or {}).values():
        out.append((aux["written"], int(aux.get("bytes", -1))))
    return out


def rollback(store, version_id: str) -> dict:
    """一键回退 = 以父版本再产一个 undo_<op> 版本，并删掉我们写出去的文件。

    删除只认数据库里那条产出任务登记的 (路径, 字节数)：文件不存在算"缺失"，
    字节数对不上说明它已被别人改过，那种情况一律不删。原件从来不在登记里，
    所以这条路径不可能删到原始数据。清洗与增强共用这一套。
    """
    version = store.get_version(version_id)
    if version is None:
        raise KeyError(f"未知版本 {version_id}")
    task = producing_task(store, version_id)
    if task is None:
        raise ValueError(f"版本 {version_id} 不是由任务产出的，没有可回退的写盘清单")
    report = task.get("report") or {}

    removed, skipped = [], []
    for record in report.get("written") or []:
        for path, size in _deletable(record):
            target = Path(path)
            if not target.is_file():
                skipped.append({"written": str(target), "why": "missing"})
            elif target.stat().st_size != size:
                skipped.append({"written": str(target),
                                "why": "size differs from recorded"})
            else:
                target.unlink()
                removed.append(str(target))

    parent = version.get("parent_id")
    new = store.put_version(version["dataset_id"], parent, f"undo_{version['op']}",
                            {"undid": version_id},
                            stats={"removed_files": len(removed),
                                   "skipped_files": len(skipped)}) if parent else None
    if new:
        inherit(store, parent, new)
    return {"undid": version_id, "restored": parent, "new_version": new,
            "op": version["op"], "removed_files": removed, "skipped_files": skipped,
            "annotations_kept": len(store.list_annotations(version_id))}


__all__ = ["Result", "apply", "build", "chain_of", "dropped_of", "evaluate", "inherit",
           "load_items", "preview", "producing_task", "rollback", "write_transforms"]
