"""训练/验证/测试切分。

三件事必须同时成立：按类别分层（小类不能整类掉进验证集）、按"来源组"分组
（同一立体视觉的相邻切片不能一张进训练一张进验证），以及按**字节内容**分组
（REQ-W17：逐字节重复的文件两侧各一张，测出来的精度是虚高的）。分组优先于
分层——一个组整块进同一折，类别分布只能在组的粒度上调。

分组键的优先级是定死的三级（PM seq=390 约束 1）：显式 `meta.group` →
内容组（size 分桶后的 sha256） → 文件名前缀 `group_key`。分层算法本身没动。
"""

from __future__ import annotations

import random
from collections import defaultdict
from typing import Optional, Sequence

from . import content
from .base import Dataset, Sample, group_key

FOLDS = ("train", "val", "test")


def dominant_label(sample: Sample) -> str:
    """一个样本可能带多个类，取出现最多的那个作为分层键。"""
    if not sample.anns:
        return "(无标注)"
    counts: dict = defaultdict(int)
    for a in sample.anns:
        counts[a.label or "(未分类)"] += 1
    return max(sorted(counts), key=lambda k: counts[k])


def _name_key(sample: Sample) -> str:
    """第一/三级键：显式 meta.group 优先，其次文件名前缀（既有行为）。"""
    g = sample.meta.get("group") if sample.meta else None
    return str(g) if g else group_key(sample.uri)


def _groups(samples: Sequence[Sample], cmap: Optional[dict] = None) -> dict:
    """REQ-W17 三级分组键的落地：显式 meta.group > 内容组 > 文件名前缀。

    实现用并查集，为的是把行为变更压到最小——
    * 没有字节重复的数据集：每个桶的键与合并顺序和旧实现**完全一致**
      （同一 stack 的切片本就共享文件名键，内容键不参与，不引入新排序抖动）；
    * 出现重复时才合并，且合并后的键取两侧字典序小者，保证同输入同划分。
    带显式 meta.group 的样本不参与内容合并：那是用户手工锁定的分组语义，
    优先级最高（PM seq=390 约束 1 的次序不是随便排的）。
    """
    cmap = cmap or {}
    parent: dict = {}

    def find(k: str) -> str:
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)     # 根取字典序小者：确定性

    tok_owner: dict = {}
    for s in samples:
        key = find(_name_key(s))
        locked = bool(s.meta and s.meta.get("group"))
        token = cmap.get(str(s.uri))
        if token and not locked:
            first = tok_owner.setdefault(token, key)
            union(first, key)
    out: dict = defaultdict(list)
    for s in samples:
        out[find(_name_key(s))].append(s)
    return dict(out)


def plan_split(ds: Dataset, ratios: Sequence[float] = (0.7, 0.15, 0.15), seed: int = 0,
               stratify: bool = True, by_group: bool = True,
               fold_of: Optional[dict] = None) -> dict:
    """返回 {样本名: 折名}。`fold_of` 允许把已有的手工指定带进来（锁定某几张永远进测试）。"""
    if len(ratios) != len(FOLDS):
        raise ValueError(f"ratios 需要 {len(FOLDS)} 项，收到 {ratios}")
    total_ratio = float(sum(ratios))
    if total_ratio <= 0:
        raise ValueError("ratios 之和必须大于 0")
    weights = [r / total_ratio for r in ratios]

    samples = list(ds.samples)
    fixed: dict = {}
    if fold_of:
        fixed = {s.name: fold_of[s.name] for s in samples
                 if fold_of.get(s.name) in FOLDS}
    pool = [s for s in samples if s.name not in fixed]

    buckets: dict = defaultdict(list)
    if by_group:
        # 内容分组对全池算一次；size 唯一文件不 IO，sha 结果有指纹缓存，
        # 热导出不再重读（REQ-W17 约束 2 的性能口径）。
        cmap = content.content_map([s.uri for s in pool])
        for key, members in _groups(pool, cmap).items():
            buckets[key] = members
    else:
        buckets = {s.name: [s] for s in pool}

    counts = {f: 0 for f in FOLDS}
    for name in fixed:
        counts[fixed[name]] += 1
    total = len(samples) or 1

    keys = sorted(buckets, key=lambda k: (dominant_label(buckets[k][0]) if stratify else "",
                                          -len(buckets[k]), k))
    if not stratify:
        random.Random(seed).shuffle(keys)

    assigned: dict = dict(fixed)
    for key in keys:
        members = buckets[key]
        fold = _least_full(counts, weights, total)
        for m in members:
            assigned[m.name] = fold
        counts[fold] += len(members)
    return {s.name: assigned.get(s.name, FOLDS[0]) for s in samples}


def _least_full(counts: dict, weights: Sequence[float], total: int) -> str:
    """挑"离目标比例缺口最大"的那一折，等价于最大余数法的逐步版本。"""
    best, best_gap = FOLDS[0], float("-inf")
    for f, w in zip(FOLDS, weights):
        gap = total * w - counts[f]
        if gap > best_gap:
            best, best_gap = f, gap
    return best


def folds_of(assignment: dict, names: Sequence[str]) -> dict:
    out: dict = {f: [] for f in FOLDS}
    for name in names:
        out[assignment.get(name, FOLDS[0])].append(name)
    return {f: sorted(v) for f, v in out.items()}


def write_split_files(out_dir, assignment: dict, names: Sequence[str],
                      subdir: str = "ImageSets/Main") -> dict:
    """把切分落成训练框架通用的目录式清单（YOLO 的 .txt 列表、VOC 的 ImageSets）。"""
    from pathlib import Path

    target = Path(out_dir) / subdir
    target.mkdir(parents=True, exist_ok=True)
    table = folds_of(assignment, names)
    (target / "all.txt").write_text("\n".join(sorted(names)) + "\n", encoding="utf-8")
    for fold, items in table.items():
        (target / f"{fold}.txt").write_text("\n".join(items) + ("\n" if items else ""),
                                            encoding="utf-8")
    return {f: len(v) for f, v in table.items()}


def summary(assignment: dict) -> dict:
    counts: dict = defaultdict(int)
    for fold in assignment.values():
        counts[fold] += 1
    total = sum(counts.values()) or 1
    return {"n": total, "counts": dict(counts),
            "ratios": {f: round(counts.get(f, 0) / total, 4) for f in FOLDS}}


def provenance(ds: Dataset) -> dict:
    """导出报告的可见判据（REQ-W17 约束 4）：这次划分是否感知重复内容、
    感知到几组。走 content 的指纹缓存，热导出零重读。"""
    return content.group_stats([s.uri for s in ds.samples])


__all__ = ["FOLDS", "plan_split", "folds_of", "write_split_files", "summary", "provenance",
           "dominant_label", "group_key"]
