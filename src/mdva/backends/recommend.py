"""推荐器：按领域/任务/机器档位给后端排序，理由全部来自实测表。

界面那句"为什么推荐它"必须是 `bench.run()` 测出来的数字，不能是写死的文案——
写死的推荐在答辩时一问就穿，而且换一批数据就不会变。
未实测的后端会被明确扣分并标注"未经实测"，不给它蒙混过关的机会。
"""

from __future__ import annotations

from typing import Optional, Sequence

from .base import BACKENDS
from .bench import default_table_path, load_table
from .probe import Probe, detect

WEIGHTS = {"f1": 0.55, "latency": 0.2, "fit": 0.15, "prompt": 0.10}
UNMEASURED_PENALTY = 0.55
ZERO_F1_PENALTY = 0.35
LATENCY_BUDGET_MS = {"high": 60.0, "mid": 120.0, "low": 300.0}


def rows(table: Sequence[dict], domain: str = "", task: str = "", modality: str = "",
         backend: str = "") -> list:
    out = []
    for row in table or []:
        if domain and row.get("category") not in (domain, "*"):
            continue
        if task and row.get("task") != task:
            continue
        if modality and row.get("modality") not in (modality, "*"):
            continue
        if backend and row.get("backend") != backend:
            continue
        out.append(row)
    return out


def best_row(table: Sequence[dict], backend: str, task: str, domain: str = "") -> Optional[dict]:
    exact = rows(table, domain, task, "", backend)
    if exact:
        return max(exact, key=lambda r: r.get("f1", 0.0))
    relaxed = [r for r in rows(table, "", task, "", backend)]
    return max(relaxed, key=lambda r: r.get("f1", 0.0)) if relaxed else None


def _fit(manifest, domain: str, modality: str, task: str) -> tuple:
    score = 1.0
    notes = []
    if domain and manifest.domains != ("*",) and domain not in manifest.domains:
        score -= 0.45
        notes.append(f"未声明适配领域 {domain}")
    if modality and modality not in manifest.modalities and "*" not in manifest.modalities:
        score -= 0.5
        notes.append(f"不处理 {modality}")
    if task and task not in manifest.tasks and "*" not in manifest.tasks:
        score -= 0.4
        notes.append(f"不担任 {task}")
    return max(0.0, score), notes


def score(backend: str, task: str = "", domain: str = "", modality: str = "",
          table: Optional[Sequence[dict]] = None, probe: Optional[Probe] = None) -> dict:
    """单个后端的打分与理由。理由文本里每个数字都能追溯到 bench 表。"""
    entry = BACKENDS.get(backend)
    inst = entry.create()
    m = inst.manifest
    probe = probe or detect()
    table = table if table is not None else load_table(default_table_path())
    ok, why = inst.available(probe)

    row = best_row(table, backend, task, domain)
    f1 = float(row.get("f1", 0.0)) if row else 0.0
    latency = float(row.get("latency_ms", 0.0)) if row else 0.0
    fit, fit_notes = _fit(m, domain, modality or (row or {}).get("modality", ""), task)
    budget = LATENCY_BUDGET_MS.get(probe.tier(), 120.0)
    speed = 1.0 if latency <= 0 else max(0.0, min(1.0, budget / max(latency, 0.01)))
    prompt = 1.0 if m.supports_prompt else 0.4

    total = (WEIGHTS["f1"] * f1 + WEIGHTS["latency"] * speed + WEIGHTS["fit"] * fit
             + WEIGHTS["prompt"] * prompt)
    if row and f1 <= 0.0:
        # 快而不产出标注的后端不能靠延迟分排到前面：标注任务上 F1=0 就是不能用
        total *= ZERO_F1_PENALTY
    reasons = []
    if row:
        kind = row.get("kind", "pr")
        metric = ("成对判别" if kind == "pairwise" else "P/R")
        reasons.append(f"{domain or '通用'}·{task or m.tasks[0]} 实测 F1 {f1:.2f}"
                       f"（{metric} P {row.get('precision', 0):.2f} / R {row.get('recall', 0):.2f}，"
                       f"{row.get('n_items', row.get('n_truth', 0))} 路）")
        reasons.append(f"单路耗时 {latency:.1f} ms（本机档位 {probe.tier()}，预算 {budget:.0f} ms）")
        if f1 <= 0.0:
            reasons.append("实测 F1 为 0：这个任务上它给不出可用标注，得分已下调")
    else:
        total *= UNMEASURED_PENALTY
        reasons.append("本机上还没实测过该任务，得分已按未实测打折")
    if row and row.get("note"):
        reasons.append(str(row["note"]))
    reasons.extend(fit_notes)
    if m.needs_gpu or m.needs_weights:
        total = 0.0 if not ok else total
        reasons.append("需显卡与权重，本平台不接入")
    if not ok:
        reasons.append(why)

    return {"backend": backend, "title": m.title, "family": m.family, "task": task,
            "domain": domain, "score": round(total, 4), "f1": round(f1, 4),
            "precision": round(float(row.get("precision", 0.0)) if row else 0.0, 4),
            "recall": round(float(row.get("recall", 0.0)) if row else 0.0, 4),
            "latency_ms": round(latency, 3), "measured": bool(row),
            "available": ok, "reason": why, "needs_gpu": m.needs_gpu,
            "reasons": reasons, "manifest": m.to_json()}


def rank(domain: str = "", task: str = "", modality: str = "",
         table: Optional[Sequence[dict]] = None, probe: Optional[Probe] = None,
         include_unavailable: bool = False) -> list:
    """全部后端的排序结果。不可用的（深度学习占位）默认不显示。"""
    out = []
    for entry in BACKENDS:
        item = score(entry.name, task, domain, modality, table, probe)
        if not item["available"] and not include_unavailable:
            continue
        out.append(item)
    return sorted(out, key=lambda r: (-r["score"], r["backend"]))


def top3(domain: str = "", task: str = "", probe: Optional[Probe] = None,
         table: Optional[Sequence[dict]] = None, k: int = 3) -> list:
    return rank(domain, task, "", table, probe)[:max(1, int(k))]


def usable(backend: str, task: str = "", domain: str = "", min_f1: float = 0.90,
           table: Optional[Sequence[dict]] = None, probe: Optional[Probe] = None) -> tuple:
    """"指定后端"的准入门槛：实测 F1 达不到 min_f1 就禁用，并把实测值显示出来。

    没实测过的后端也算不通过——界面这时提示"先跑一次评测"，
    而不是给一个未经证实的算法开绿灯。
    """
    item = score(backend, task, domain, "", table, probe)
    if not item["available"]:
        return (False, item["f1"], item["reason"] or "该后端不可用")
    if not item["measured"]:
        return (False, item["f1"],
                f"尚未实测（{domain or '通用'}·{task or '默认任务'}），"
                f"先运行 mdva bench 再指定该后端")
    if item["f1"] < min_f1:
        return (False, item["f1"],
                f"实测 F1 {item['f1']:.2f} < 门槛 {min_f1:.2f}"
                f"（P {item['precision']:.2f} / R {item['recall']:.2f}），已禁用")
    return (True, item["f1"], f"实测 F1 {item['f1']:.2f} ≥ 门槛 {min_f1:.2f}")


__all__ = ["LATENCY_BUDGET_MS", "UNMEASURED_PENALTY", "WEIGHTS", "ZERO_F1_PENALTY",
           "best_row", "rank", "rows", "score", "top3", "usable"]
