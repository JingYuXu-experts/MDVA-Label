"""视觉提示传播与并行预标注（L4 智能标注）。

三条路径共用同一个后端接口：
`same_image` 同图找相似、`propagate` 跨图传播、`preannotate` 整批预标注。
并行走 `concurrent.futures`，workers/executor 两个旋钮与 L3 批量流水线一致，
所以"加速比"这件事在标注和在同一套度量口径上（`measure_speedup`）。

写入数据库时**默认产一个新版本**（op=preannotate），不覆盖人工已经标的东西：
预标注是可重跑的机器产物，覆盖人工成果是不可接受的，回退也只是切回父版本。
"""

from __future__ import annotations

import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from ..backends.base import BACKENDS, Prompt
from ..core.model import Slice
from .model import Ann, merge

DEFAULT_WORKERS = 0
MAX_WORKERS_CAP = 8


@dataclass
class Job:
    uri: str
    backend: str
    params: dict = field(default_factory=dict)
    prompts: list = field(default_factory=list)
    window: Optional[tuple] = None
    index: int = 0
    modality: Optional[str] = None

    def slice(self) -> Slice:
        if self.window:
            return Slice(index=int(self.index), t0=float(self.window[0]), t1=float(self.window[1]))
        return Slice(index=int(self.index))


@dataclass
class Result:
    uri: str
    anns: list = field(default_factory=list)
    ok: bool = True
    error: str = ""
    elapsed: float = 0.0
    modality: str = ""

    @property
    def count(self) -> int:
        return len(self.anns)


def _work(job: Job) -> Result:
    """worker 体。必须是模块级函数、参数可序列化，否则进程池不可用。"""
    from .. import io as MDIO

    t0 = time.perf_counter()
    try:
        item = MDIO.load_any(job.uri, modality=job.modality)
        backend = BACKENDS.get(job.backend).create(**(job.params or {}))
        if not backend.handles(item):
            return Result(job.uri, ok=True, error="", elapsed=time.perf_counter() - t0,
                          modality=item.modality)
        anns = backend.predict(item, job.prompts, job.slice())
        return Result(job.uri, list(anns), True, "", time.perf_counter() - t0, item.modality)
    except Exception as exc:      # noqa: BLE001 - 并行批处理里一路失败不能带崩整批
        return Result(job.uri, ok=False, error=f"{type(exc).__name__}: {exc}",
                      elapsed=time.perf_counter() - t0)


def _resolve_workers(workers: int, n_jobs: int) -> int:
    if workers == 0:
        workers = min(MAX_WORKERS_CAP, max(1, _cpu_count() // 2), n_jobs)
    return max(1, min(int(workers), n_jobs))


def _map(jobs: Sequence[Job], workers: int = 0, executor: str = "thread") -> tuple:
    """按并发数派发，返回 (结果, 实际并发数)。

    workers<=1 时走主线程：省掉池开销，断点也能停住——调试并行代码时这很值钱。
    """
    if not jobs:
        return [], 0
    n = _resolve_workers(workers, len(jobs))
    if n == 1:
        return [_work(j) for j in jobs], 1
    if executor == "process":
        with ProcessPoolExecutor(max_workers=n) as pool:
            return list(pool.map(_work, jobs)), n
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(_work, jobs)), n


def _cpu_count() -> int:
    import os

    return os.cpu_count() or 1


def resolve_prompt(item_or_uri, prompt: Prompt, params: Optional[dict] = None) -> Prompt:
    """把"在源图上框的一块"变成自带模板的提示，之后就能脱离源图传播。"""
    if prompt.template is not None:
        return prompt
    from .. import io as MDIO
    from ..backends.visual import _template_pair

    item = item_or_uri if hasattr(item_or_uri, "modality") else MDIO.load_any(str(item_or_uri))
    point_size = int((params or {}).get("point_size", 15))
    tpl, _rgb = _template_pair(item, prompt, Slice.full(), point_size)
    return Prompt(prompt.kind, list(prompt.coords), prompt.label, prompt.role,
                  str(getattr(item, "uri", "")), tpl)


def same_image(item, prompt: Prompt, backend: str = "visual_prompt",
               params: Optional[dict] = None, window: Optional[Slice] = None) -> list:
    """画布上框一个 → 同图其余同类目标自动补齐。"""
    inst = BACKENDS.get(backend).create(**(params or {}))
    return inst.predict(item, [prompt], window)


def propagate(uris: Iterable[str], prompt: Prompt, source=None, backend: str = "visual_prompt",
              params: Optional[dict] = None, workers: int = DEFAULT_WORKERS,
              executor: str = "thread", window: Optional[tuple] = None,
              index: int = 0, min_confidence: float = 0.0) -> dict:
    """把一个提示传播到多路数据，返回 {uri: [Ann]} 与耗时统计。

    `source` 是画提示那张图（数据项或路径）。模板只从源图取一次，
    之后每张目标图独立匹配，所以目标图完全不需要见过源图。
    """
    uris = [str(u) for u in uris]
    origin = source
    if origin is None:
        origin = uris[0] if uris else None
    filled = resolve_prompt(origin, prompt, params)
    jobs = [Job(uri, backend, dict(params or {}), [filled], window, index) for uri in uris]
    t0 = time.perf_counter()
    results, used = _map(jobs, workers, executor)
    wall = time.perf_counter() - t0
    kept = []
    for res in results:
        res.anns = [a for a in res.anns if a.confidence >= min_confidence]
        kept.append(res)
    return {"results": {r.uri: r.anns for r in kept}, "records": kept,
            "n_items": len(kept), "n_ok": sum(1 for r in kept if r.ok),
            "n_annotations": sum(r.count for r in kept),
            "errors": {r.uri: r.error for r in kept if not r.ok},
            "wall_ms": wall * 1000.0, "sum_ms": sum(r.elapsed for r in kept) * 1000.0,
            "workers": used, "requested_workers": workers, "executor": executor,
            "speedup": _speedup(sum(r.elapsed for r in kept), wall)}


def preannotate(uris: Iterable[str], backend: str, params: Optional[dict] = None,
                workers: int = DEFAULT_WORKERS, executor: str = "thread",
                window: Optional[tuple] = None, index: int = 0,
                prompts: Sequence[Prompt] = (), min_confidence: float = 0.0) -> dict:
    """整批预标注：经典后端不接提示，视觉提示后端需要 prompts 里带上模板。"""
    jobs = [Job(str(u), backend, dict(params or {}), list(prompts), window, index)
            for u in uris]
    t0 = time.perf_counter()
    results, used = _map(jobs, workers, executor)
    wall = time.perf_counter() - t0
    for res in results:
        res.anns = [a for a in res.anns if a.confidence >= min_confidence]
    return {"results": {r.uri: r.anns for r in results}, "records": results,
            "n_items": len(results), "n_ok": sum(1 for r in results if r.ok),
            "n_annotations": sum(r.count for r in results),
            "errors": {r.uri: r.error for r in results if not r.ok},
            "wall_ms": wall * 1000.0, "sum_ms": sum(r.elapsed for r in results) * 1000.0,
            "workers": used, "requested_workers": workers, "executor": executor,
            "speedup": _speedup(sum(r.elapsed for r in results), wall)}


def _speedup(serial_ms: float, wall_ms: float) -> float:
    if wall_ms <= 0:
        return 1.0
    return round(serial_ms / wall_ms, 3)


def measure_speedup(uris: Iterable[str], backend: str, params: Optional[dict] = None,
                    workers: int = 4, executor: str = "thread",
                    window: Optional[tuple] = None) -> dict:
    """同一批输入跑 1 路 vs N 路，加速比必须实测而不是推算。"""
    uris = [str(u) for u in uris]
    serial = preannotate(uris, backend, params, workers=1, executor=executor, window=window)
    parallel = preannotate(uris, backend, params, workers=workers, executor=executor,
                           window=window)
    speedup = round(serial["wall_ms"] / max(parallel["wall_ms"], 0.001), 3)
    return {"workers": workers, "executor": executor, "n_items": len(uris),
            "serial_ms": round(serial["wall_ms"], 2), "parallel_ms": round(parallel["wall_ms"], 2),
            "speedup": speedup, "efficiency": round(speedup / max(1, workers), 3),
            "n_annotations": parallel["n_annotations"],
            "consistent": parallel["n_annotations"] == serial["n_annotations"]}


# ---------------------------------------------------------------------- 落库

def apply_results(store, version_id: str, results: dict, role: str = "annotator",
                  new_version: bool = True, op: str = "preannotate",
                  dedupe: bool = True, iou_thr: float = 0.9,
                  state: str = "draft") -> dict:
    """把预标注/传播结果写进数据库。

    默认开新版本：人工已经改过的东西不被机器覆盖，回退只需要把 head 指回父版本，
    原件与旧版本都还在（这是全平台统一的破坏性操作纪律）。
    """
    version = store.get_version(version_id)
    if version is None:
        raise KeyError(f"未知版本 {version_id}")
    target = version_id
    if new_version:
        target = store.put_version(version["dataset_id"], version_id, op,
                                   {"source_version": version_id})

    added = skipped = merged = 0
    for uri, anns in (results.get("results") or {}).items():
        # 去重要连着父版本一起看：重跑预标注不能把同一批框再堆一遍，
        # 也不能因为新版本是空的就假装"这里还没有标注"。
        existing = list(store.list_annotations(version_id, uri))
        if target != version_id:
            existing += list(store.list_annotations(target, uri))
        existing = [row["ann"] for row in existing]
        pool = existing + list(anns)
        keep = merge(pool, iou_thr) if dedupe else list(anns)
        if dedupe and existing:
            merged += max(0, len(pool) - len(keep))
        fresh = [a for a in keep if not any(_same(a, e) for e in existing)]
        for ann in fresh:
            store.put_annotation(target, uri, ann, role=role, state=state)
            added += 1
        skipped += len(anns) - len(fresh)
    report = {"version": target, "version_is_new": target != version_id, "added": added,
              "deduped": merged, "skipped_already_present": skipped,
              "n_items": len(results.get("results") or {})}
    if new_version:
        report["carried"] = _carry(store, version_id, target)
        task = store.create_task(op, version["dataset_id"], version_id, target,
                                 {"added": added, "workers": results.get("workers")})
        store.update_task(task, "done", report, version_out=target)
        report["task_id"] = task
    return report


def _carry(store, from_version: str, to_version: str) -> int:
    """把父版本的标注原样带进新版本，审核状态与提交人一起走。

    新版本是"当前工作集"而不是机器结果的增量：只落机器产出的话，用户刚标完的人工框
    会在点一下批量预标注之后凭空消失，回退也等于让他重标。与清洗/增强的 inherit 同一纪律。
    """
    have = [row["ann"] for row in store.list_annotations(to_version)]
    n = 0
    for row in store.list_annotations(from_version):
        if any(_same(row["ann"], a) for a in have):
            continue
        store.put_annotation(to_version, row["item_uri"], row["ann"],
                             role=row["role"], state=row["state"])
        have.append(row["ann"])
        n += 1
    return n


def _same(a: Ann, b: Ann) -> bool:
    return (a.geom == b.geom and a.label == b.label and a.slice == b.slice
            and len(a.coords) == len(b.coords)
            and all(abs(x - y) < 1e-6 for x, y in zip(a.coords, b.coords)))


def undo_last(store, version_id: str) -> dict:
    """回退 = 把 head 指回父版本。机器版本与其中的标注都不删除。"""
    version = store.get_version(version_id)
    if version is None:
        raise KeyError(f"未知版本 {version_id}")
    parent = version.get("parent_id")
    return {"from": version_id, "to": parent, "op": version.get("op"),
            "kept": version_id, "annotations": len(store.list_annotations(version_id))}


def score_items(uris: Iterable[str], backend: str, prompts: Sequence[Prompt] = (),
                params: Optional[dict] = None, workers: int = DEFAULT_WORKERS,
                executor: str = "thread", window: Optional[tuple] = None) -> list:
    """每一路的最高置信度，用于内容过滤与主动学习排序。"""
    out = []
    for res in preannotate(uris, backend, params, workers, executor, window,
                           prompts=prompts)["records"]:
        best = max((a.confidence for a in res.anns), default=0.0)
        out.append({"uri": res.uri, "score": round(best, 4), "n": res.count,
                    "ok": res.ok, "error": res.error})
    return sorted(out, key=lambda r: -r["score"])


__all__ = ["Job", "Result", "apply_results", "measure_speedup", "preannotate", "propagate",
           "resolve_prompt", "same_image", "score_items", "undo_last"]
