"""主动学习闭环：把最可疑的样本优先送标，再用真值检查"标得越多是否越准"。

不确定性来自经典后端自己的输出（越界幅度、检测密度），不是深度模型的 logits——
这是"完全不碰大模型"这条决定下唯一诚实的做法，界面上也要照这个措辞写。

两条纪律：
1. 每轮标注只**新增版本**，旧版本与原件都不动，任何一轮都能回退；
2. 指定后端必须先过门槛（默认实测 F1 ≥ 0.90），达不到就拒绝开跑并显示实测值。
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional, Sequence

from ..backends import bench as BN
from ..backends.base import BACKENDS, Prompt
from ..backends.bench import _match_intervals, _match_points
from ..core.model import Slice
from .model import Ann


def uncertainty(uris: Iterable[str], backend: str, params: Optional[dict] = None,
                window: Optional[tuple] = None, workers: int = 0,
                executor: str = "thread") -> list:
    """每一路的可疑度排序。score 越大越该先给人看。"""
    from .. import io as MDIO

    out = []
    for uri in uris:
        try:
            item = MDIO.load_any(str(uri))
            inst = BACKENDS.get(backend).create(**(params or {}))
            if not inst.handles(item):
                out.append({"uri": str(uri), "score": 0.0, "reason": "模态不匹配",
                            "modality": item.modality})
                continue
            sl = Slice.at_time(*window) if window else None
            if hasattr(inst, "uncertainty"):
                score = inst.uncertainty(item, sl)
            else:
                score = None
            if score is None:
                anns = inst.predict(item, (), sl)
                score = min(1.0, len(anns) / 40.0)
                reason = "检测密度"
            else:
                reason = ("越界幅度" if getattr(inst, "analyzer", "") in
                          ("dynamic_threshold", "changepoint", "rolling_stats", "iforest")
                          else "检测密度")
            out.append({"uri": str(uri), "score": round(float(score), 4), "reason": reason,
                        "modality": item.modality})
        except Exception as exc:      # noqa: BLE001 - 单路读不出来不影响其它路排序
            out.append({"uri": str(uri), "score": 0.0, "reason": f"读取失败：{exc}"})
    out.sort(key=lambda r: (-r["score"], r["uri"]))
    return out


def pick_round(ranked: Sequence[dict], budget: int, already_labeled: Iterable[str] = (),
               min_score: float = 0.0) -> list:
    """从排序里取本轮要标的样本，跳过已经标过的。"""
    done = set(already_labeled or ())
    pool = [r for r in ranked if r["uri"] not in done and r["score"] >= min_score]
    return pool[: max(0, int(budget))]


def evaluate_labels(labeled: dict, truth: dict, task: str,
                    tol: float = BN.DEFAULT_TOL_PX, iou: float = BN.DEFAULT_IOU) -> dict:
    """把"已经标好的这批"当预测结果对真值打分——E3 曲线的纵轴。

    labeled: {uri: [Ann]}，uri 必须能对回 truth 的数据项。
    """
    items = []
    for uri in labeled:
        try:
            from .. import io as MDIO

            items.append(MDIO.load_any(str(uri)))
        except Exception:
            continue
    by_uri = {str(i.uri): i for i in items}
    if task in ("series_anomaly", "interval", "changepoint"):
        return _labels_vs_intervals(labeled, by_uri, truth, iou)
    if task in ("detect_source", "reject_cosmic"):
        return _labels_vs_points(labeled, by_uri, _cosmic(truth, task), tol)
    return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "n_labeled": len(labeled),
            "n_truth": 0, "kind": "skipped", "note": f"{task} 没有可用的标注评测量规"}


def _cosmic(truth: dict, task: str) -> dict:
    return BN._cosmic_view(truth) if task == "reject_cosmic" else truth


def _labels_vs_intervals(labeled: dict, by_uri: dict, truth: dict, iou: float) -> dict:
    injected = truth.get("injected_anomalies") or {}
    tp = fp = fn = 0
    for uri, anns in labeled.items():
        item = by_uri.get(str(uri))
        if item is None or not injected:
            continue
        ts = item.load()
        preds = [BN._pred_interval(a) for a in anns if a.geom in ("interval", "level")]
        truths = []
        for channel, kinds in injected.items():
            if channel not in getattr(ts, "channels", []):
                continue
            ranges = [r for group in kinds.values() for r in group]
            truths += [(a, b) for a, b, *_ in BN._truth_times(item, ranges)]
        t2, f2, n2 = _match_intervals(preds, truths, iou)
        tp, fp, fn = tp + t2, fp + f2, fn + n2
    p, r, f1 = BN._pr(tp, fp, fn)
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f1, 4),
            "n_labeled": tp + fp, "n_truth": tp + fn, "kind": "pr",
            "note": f"标注 {tp} 命中 / {fp} 误标 / 漏 {fn}"}


def _labels_vs_points(labeled: dict, by_uri: dict, truth: dict, tol: float) -> dict:
    table = truth.get("payload_truth") or {}
    tp = fp = fn = 0
    for uri, anns in labeled.items():
        entry = _entry_for(table, str(uri), by_uri)
        if not entry:
            continue
        truths = BN._positions(entry.get("centroids") or [])
        preds = []
        for a in anns:
            if a.geom == "point":
                preds.append((a.coords[0], a.coords[1]))
            elif a.geom == "box":
                preds.append(((a.coords[0] + a.coords[2]) / 2.0,
                              (a.coords[1] + a.coords[3]) / 2.0))
        t2, f2, n2 = _match_points(preds, truths, tol)
        tp, fp, fn = tp + t2, fp + f2, fn + n2
    p, r, f1 = BN._pr(tp, fp, fn)
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f1, 4),
            "n_labeled": tp + fp, "n_truth": tp + fn, "kind": "pr",
            "note": f"标注 {tp} 命中 / {fp} 误标 / 漏 {fn}"}


def _entry_for(table: dict, uri: str, by_uri: dict):
    """按数据项名找真值条目：先精确同干名，再退回到模糊匹配（切片展开的名字带后缀）。"""
    from pathlib import Path

    stem = Path(uri).stem
    if stem in table:
        return table[stem]
    item = by_uri.get(uri)
    if item is not None:
        return BN._truth_entry(table, item)
    for name, entry in table.items():
        if name in stem:
            return entry
    return None


class SimulatedAnnotator:
    """用真值当"人来标"，让 E3（已标比例–精度曲线）能自动跑完。

    只用于实验与演示：界面里真人标注走的是画布，不会调用它。
    故意在模拟里掺入噪声（漏标/位置抖动），否则曲线必然是漂亮的 100%，
    那种数字没有说服力。
    """

    def __init__(self, truth: dict, task: str, jitter_px: float = 2.0, drop: float = 0.12,
                 seed: int = 0):
        import numpy as np

        self.truth, self.task = truth, task
        self.rng = np.random.default_rng(seed)
        self.jitter, self.drop = jitter_px, drop

    def __call__(self, uri: str) -> list:
        from pathlib import Path

        stem = Path(uri).stem
        anns: list = []
        if self.task in ("series_anomaly", "interval", "changepoint"):
            # 真值是采样序号区间，标注必须写回绝对时刻，否则评精度时对不上
            try:
                from .. import io as MDIO

                item = MDIO.load_any(str(uri))
            except Exception:
                return []
            for channel, kinds in (self.truth.get("injected_anomalies") or {}).items():
                ranges = [r for group in kinds.values() for r in group]
                for a, b, label in BN._truth_times(item, ranges):
                    if self.rng.random() < self.drop:
                        continue
                    anns.append(Ann("interval", [a, b], str(label), source="manual",
                                    confidence=1.0))
            return anns
        entry = (self.truth.get("payload_truth") or {}).get(stem) or {}
        for x, y in BN._positions(entry.get("centroids") or []):
            if self.rng.random() < self.drop:
                continue
            dx = float(self.rng.normal(0, self.jitter))
            dy = float(self.rng.normal(0, self.jitter))
            anns.append(Ann("box", [x - 8 + dx, y - 8 + dy, x + 8 + dx, y + 8 + dy],
                            "target", source="manual", confidence=1.0))
        return anns


def run_loop(store, version_id: str, uris: Sequence[str], backend: str, oracle: Callable,
             truth: dict, task: str, domain: str = "", rounds: int = 4, budget: int = 5,
             params: Optional[dict] = None, min_f1: float = 0.90, table: Optional[list] = None,
             window: Optional[tuple] = None, workers: int = 0,
             enforce_gate: bool = True) -> dict:
    """多轮"挑最可疑 → 标注 → 用真值验精度"，返回每一轮的记录。

    oracle(uri) -> [Ann]：实验里是 SimulatedAnnotator，真实使用时应换成人工结果。
    """
    from ..backends.recommend import usable

    version = store.get_version(version_id)
    if version is None:
        raise KeyError(f"未知版本 {version_id}")

    gate = usable(backend, task, domain, min_f1, table) if enforce_gate else (True, None, "跳过门槛")
    history = []
    current = version_id
    labeled: dict = {}
    if enforce_gate and not gate[0]:
        return {"ok": False, "gate": {"backend": backend, "passed": False, "f1": gate[1],
                                      "reason": gate[2]}, "history": [],
                "stop_advice": "未通过准入门槛，先换后端或降低门槛"}

    ranked = {r["uri"]: r for r in uncertainty(uris, backend, params, window, workers)}
    order = [r["uri"] for r in sorted(ranked.values(), key=lambda r: (-r["score"], r["uri"]))]

    for i in range(max(1, int(rounds))):
        chosen = pick_round([ranked[u] for u in order], int(budget), labeled.keys())
        if not chosen:
            break
        picked = []
        round_version = store.put_version(version["dataset_id"], current,
                                          f"active_round_{i + 1}",
                                          {"backend": backend, "budget": int(budget)})
        for row in chosen:
            anns = list(oracle(row["uri"]) or [])
            labeled[row["uri"]] = anns
            for ann in anns:
                store.put_annotation(round_version, row["uri"], ann, role="annotator",
                                     state="submitted")
            picked.append({"uri": row["uri"], "uncertainty": row["score"],
                           "reason": row.get("reason", ""), "n_annotations": len(anns)})
        current = round_version
        score = evaluate_labels(labeled, truth, task)
        score.update({"round": i + 1, "labeled_items": len(labeled),
                      "labeled_ratio": round(len(labeled) / max(1, len(order)), 4),
                      "picked": picked})
        history.append(score)
        advice = stop_advice(history)
        if advice:
            break

    return {"ok": True, "backend": backend, "task": task, "version": current,
            "gate": {"backend": backend, "passed": True, "f1": gate[1], "reason": gate[2]},
            "history": history, "n_unlabeled": max(0, len(order) - len(labeled)),
            "ranking_sample": [ranked[u] for u in order[:5]],
            "stop_advice": stop_advice(history) or "精度仍在上升，可继续"}


def stop_advice(history: Sequence[dict]) -> str:
    """连续两轮精度不升反降就建议停手——再多标也只是浪费时间。"""
    if len(history) < 3:
        return ""
    f1s = [h["f1"] for h in history[-3:]]
    if f1s[-1] <= f1s[-2] <= f1s[-3] and f1s[-1] < 1.0:
        return (f"近三轮 F1 {f1s[0]:.2f}→{f1s[1]:.2f}→{f1s[2]:.2f} 没有提升，"
                f"建议停止主动学习并改用全量标注或换后端")
    return ""


def hard_examples(store, version_id: str, uris: Sequence[str], backend: str,
                  k: int = 10, params: Optional[dict] = None,
                  window: Optional[tuple] = None, workers: int = 0) -> dict:
    """难例回流：被驳回的、以及后端自己最没底的那几路，重新排队。"""
    rejected = sorted({row["item_uri"] for row in store.list_annotations(version_id)
                       if row["state"] == "rejected"})
    ranked = uncertainty(uris, backend, params, window, workers)
    pool = [{"uri": r["uri"], "score": r["score"], "why": r.get("reason", ""),
             "source": "uncertainty"} for r in ranked]
    for uri in rejected:
        pool.append({"uri": uri, "score": 1.0, "why": "审核驳回", "source": "rejected"})
    pool.sort(key=lambda r: (-r["score"], r["uri"]))
    return {"k": int(k), "n_rejected": len(rejected), "queue": pool[: max(1, int(k))]}


def propagate_with_gate(store, version_id: str, uris: Sequence[str], prompt: Prompt,
                        backend: str = "visual_prompt", params: Optional[dict] = None,
                        workers: int = 0, min_f1: float = 0.90, domain: str = "",
                        table: Optional[list] = None, task: str = "detect_similar",
                        enforce_gate: bool = False, source=None) -> dict:
    """带门槛的批量传播：门槛不过就别写库，免得脏标注进版本。"""
    from ..backends.recommend import usable

    from .prompts import apply_results, propagate

    gate = usable(backend, task, domain, min_f1, table)
    if enforce_gate and not gate[0]:
        return {"ok": False, "gate": {"passed": False, "f1": gate[1], "reason": gate[2]},
                "written": None}
    result = propagate(uris, prompt, source, backend, params, workers)
    written = apply_results(store, version_id, result)
    return {"ok": True, "gate": {"passed": True, "f1": gate[1], "reason": gate[2]},
            "result": {k: v for k, v in result.items() if k != "results"},
            "counts": {u: len(a) for u, a in result["results"].items()},
            "written": written}


__all__ = ["SimulatedAnnotator", "evaluate_labels", "hard_examples", "pick_round",
           "propagate_with_gate", "run_loop", "stop_advice", "uncertainty"]
