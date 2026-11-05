"""训练/验证/测试切分。

两件事必须同时成立：按类别分层（小类不能整类掉进验证集）和按"来源组"分组
（同一立体视觉的相邻切片不能一张进训练一张进验证）。分组优先于分层——
一个组整块进同一折，类别分布只能在组的粒度上调。
"""

from __future__ import annotations

import random
from collections import defaultdict
from typing import Optional, Sequence

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


def _groups(samples: Sequence[Sample]) -> dict:
    out: dict = defaultdict(list)
    for s in samples:
        out[str(s.meta.get("group") or group_key(s.uri))].append(s)
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
        for key, members in _groups(pool).items():
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


__all__ = ["FOLDS", "plan_split", "folds_of", "write_split_files", "summary",
           "dominant_label", "group_key"]
