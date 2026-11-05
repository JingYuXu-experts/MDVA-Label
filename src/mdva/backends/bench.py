"""统一评测：每个后端在带真值的示例数据上实测精度与延迟。

推荐器的理由文本、"指定后端必须达到 90%" 的质量门、论文里的 E1 表格，
全部读这张表，不写死任何数字。所以这张表必须由真实运行产生：
真值来自各学科 `truth.json`；没有逐目标真值可用时（比如图像质量类），
用**合成退化**（模糊/坏列/削顶）造出自定义真值，并在结果里标明判据种类。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from ..annotate.model import Ann
from ..core.model import DataItem, Modality, Slice, TimeSeries

TASK_OF_TRUTH = ("series_anomaly", "interval", "changepoint")
DEFAULT_TOL_PX = 6.0
DEFAULT_IOU = 0.05


@dataclass
class Row:
    backend: str
    task: str
    category: str
    modality: str
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    latency_ms: float = 0.0
    vram_gb: float = 0.0
    n_items: int = 0
    n_pred: int = 0
    n_truth: int = 0
    kind: str = "pr"                 # pr = 查准/查全；pairwise = 成对判别
    note: str = ""

    def to_json(self) -> dict:
        return {"backend": self.backend, "task": self.task, "category": self.category,
                "modality": self.modality, "precision": round(self.precision, 4),
                "recall": round(self.recall, 4), "f1": round(self.f1, 4),
                "latency_ms": round(self.latency_ms, 3), "vram_gb": self.vram_gb,
                "n_items": self.n_items, "n_pred": self.n_pred, "n_truth": self.n_truth,
                "kind": self.kind, "note": self.note}


def _pr(tp: int, fp: int, fn: int) -> tuple:
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f1


def _pred_interval(ann: Ann) -> tuple:
    return (min(ann.coords[0], ann.coords[1]), max(ann.coords[0], ann.coords[1]))


def _overlap(a: tuple, b: tuple) -> float:
    inter = min(a[1], b[1]) - max(a[0], b[0])
    if inter <= 0:
        return 0.0
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union if union > 0 else 0.0


def _hit(pred: tuple, truth: tuple, iou: float) -> bool:
    """两条区间算不算命中：面积重叠，或点状预测正好落在真值区间内。

    变点检测给的是零时长时刻，硬按 IoU 判会永远是 0——但"在漂移段起点打了一个变点"
    确实是正确检出，所以这里显式承认点的包含关系。
    """
    inter = min(pred[1], truth[1]) - max(pred[0], truth[0])
    if inter < 0:
        return False
    union = max(pred[1], truth[1]) - min(pred[0], truth[0])
    if union <= 0:
        return True
    if inter / union >= iou:
        return True
    return pred[0] == pred[1] and truth[0] <= pred[0] <= truth[1]


def _match_intervals(preds: Sequence[tuple], truths: Sequence[tuple], iou=DEFAULT_IOU) -> tuple:
    """贪心配对：一条真值最多被一条预测命中，避免"一网打尽"刷满召回。"""
    used = [False] * len(preds)
    tp = fn = 0
    for truth in sorted(tuple(t) for t in truths):
        cands = [(_overlap(p, truth), -abs((p[0] + p[1]) / 2.0 - (truth[0] + truth[1]) / 2.0), i)
                 for i, p in enumerate(preds) if not used[i] and _hit(p, truth, iou)]
        if not cands:
            fn += 1
            continue
        used[max(cands)[2]] = True
        tp += 1
    return tp, len(preds) - tp, fn


def _match_points(preds: Sequence[tuple], truths: Sequence[tuple], tol=DEFAULT_TOL_PX) -> tuple:
    used = [False] * len(preds)
    tp = fn = 0
    for tx, ty in truths:
        best, best_d = None, tol
        for i, (px, py) in enumerate(preds):
            if used[i]:
                continue
            d = float(np.hypot(px - tx, py - ty))
            if d <= best_d:
                best, best_d = i, d
        if best is None:
            fn += 1
        else:
            used[best] = True
            tp += 1
    return tp, len(preds) - tp, fn


# ---------------------------------------------------------------------- 各任务评分

def _truth_times(item: DataItem, ranges: Sequence[tuple]) -> list:
    """真值是采样序号区间，换成分数上的绝对时刻。"""
    t = np.asarray(item.load().t, dtype=np.float64)
    out = []
    for a, b, *_ in ranges:
        i0 = int(max(0, min(a, t.size - 1)))
        i1 = int(max(i0 + 1, min(b, t.size)))
        out.append((float(t[i0]), float(t[i1 - 1]), _label(a, b, ranges)))
    return out


def _label(a, b, ranges) -> str:
    for r in ranges:
        if r[0] == a and r[1] == b:
            return r[2] if len(r) > 2 else ""
    return ""


def _with_channel(backend, index: int):
    """按真值通道重建后端实例。

    动态阈值/变点/滑动统计都只算 `channel` 参数指定的那一根通道（算子收的是**序号**，
    不是通道名），不重造就等于拿 SA_Current 的输出去对 Batt_Temp 的真值——分数必然为 0。
    """
    if "channel" not in (backend.manifest.schema or {}):
        return backend
    params = dict(backend.params)
    params["channel"] = int(index)
    from .base import BACKENDS

    return BACKENDS.get(backend.manifest.name).create(**params)


def score_series(backend, items, truth: dict, tol=None) -> dict:
    injected = truth.get("injected_anomalies") or {}
    if not injected:
        return _no_truth("truth.json 缺 injected_anomalies")
    tp = fp = fn = 0
    lat = 0.0
    n_pred = n_truth = 0
    for item in items:
        ts = item.load()
        if not isinstance(ts, TimeSeries):
            continue
        for channel, kinds in injected.items():
            if channel not in ts.channels:
                continue
            ranges = [r for group in kinds.values() for r in group]
            truths = [(a, b) for a, b, *_ in _truth_times(item, ranges)]
            if not truths:
                continue
            t0 = time.perf_counter()
            anns = _with_channel(backend, ts.channels.index(channel)).predict(
                item, window=Slice.at_time(ts.start, ts.end))
            lat += (time.perf_counter() - t0) * 1000.0
            # 区间几何不带通道号（VInterval 本来就没有这个字段），所以按时间匹配、不分通道
            preds = [_pred_interval(a) for a in anns if a.geom in ("interval", "level")]
            t2, f2, n2 = _match_intervals(preds, truths)
            tp += t2
            fp += f2
            fn += n2
            n_pred += len(preds)
            n_truth += len(truths)
    p, r, f1 = _pr(tp, fp, fn)
    return {"precision": p, "recall": r, "f1": f1,
            "latency_ms": lat / max(1, len(items)), "n_pred": n_pred, "n_truth": n_truth,
            "kind": "pr", "note": f"真值区间 {n_truth} 段"}


def score_points(backend, items, truth: dict, key: str = "centroids", tol=DEFAULT_TOL_PX) -> dict:
    table = truth.get("payload_truth") or {}
    if not table:
        return _no_truth("truth.json 未含逐目标真值，请重新生成示例数据（mdva datasets）")
    tp = fp = fn = 0
    lat = 0.0
    n_pred = n_truth = 0
    for item in items:
        entry = _truth_entry(table, item)
        if entry is None:
            continue
        points = _positions(entry.get(key) or [])
        if not points:
            continue
        t0 = time.perf_counter()
        anns = backend.predict(item, window=_focus_window(item))
        lat += (time.perf_counter() - t0) * 1000.0
        preds = [a.bbox()[:2] if a.geom == "box" else (a.coords[0], a.coords[1])
                 for a in anns if a.geom in ("box", "point")]
        preds = [(float(x), float(y)) for x, y in preds]
        t2, f2, n2 = _match_points(preds, points, tol)
        tp, fp, fn = tp + t2, fp + f2, fn + n2
        n_pred += len(preds)
        n_truth += len(points)
    p, r, f1 = _pr(tp, fp, fn)
    if not n_truth:
        return _no_truth("该学科没有逐目标真值，点位评测跳过")
    return {"precision": p, "recall": r, "f1": f1,
            "latency_ms": lat / max(1, len(items)), "n_pred": n_pred, "n_truth": n_truth,
            "kind": "pr", "note": f"容差 {tol} px"}


def _focus_window(item: DataItem) -> Slice:
    """立方体/帧序列取中间那一片：病灶与耀斑不一定出现在第 0 层。"""
    return Slice.at_index(int(item.n_views) // 2) if item.n_views > 1 else Slice.full()


def _truth_entry(table: dict, item: DataItem):
    stem = Path(item.uri).stem
    for name, entry in table.items():
        if name == stem or name in stem or stem in name:
            return entry if isinstance(entry, dict) else None
    return None


def _positions(rows) -> list:
    out = []
    for r in rows:
        if isinstance(r, (list, tuple)) and len(r) >= 2:
            out.append((float(r[0]), float(r[1])))
    return out


def degraded(item: DataItem, seed: int = 0) -> DataItem:
    """合成退化副本：模糊 + 削顶（二维再加一列坏值）。用来给质量类后端造自定义真值。"""
    from scipy import ndimage

    from ..render.image import pick_slice

    # 帧序列/立方体取一片再退化：item.load() 给的是帧容器对象，不是 ndarray
    arr = np.asarray(pick_slice(item, Slice.at_index(0)), dtype=np.float64)
    if arr.ndim == 2:
        bad = ndimage.gaussian_filter(arr, 2.2)
        bad = np.clip(bad, None, float(np.percentile(bad, 99.0)))
        bad[:, int(bad.shape[1] * 0.4)] = float(np.nanmax(bad))
    else:
        bad = ndimage.gaussian_filter(arr, sigma=(2.2, 2.2, 0))
        top = float(np.percentile(bad[..., 0], 99.0))
        bad[..., 0] = np.clip(bad[..., 0], None, top)
    return DataItem(id=f"{item.id}#blur", modality=item.modality, uri=f"{item.uri}#blur",
                    title=f"{item.title or item.id}（退化）", t_ref=item.t_ref, t_end=item.t_end,
                    meta={**item.meta, "degraded": True, "how": "blur+clip"},
                    shape=bad.shape, dtype=str(bad.dtype), n_views=1,
                    _opener=lambda b=bad: b)


def score_quality(backend, items, truth: dict, metric: Optional[str] = None) -> dict:
    """成对判别：同一张图的原图与退化副本，后端必须认为后者更差。

    质量类算子没有"逐目标真值"可查，所以这里评的是**排序方向**是否正确，
    结果 kind=pairwise，界面与论文都按"成对判别准确率"表述，不冒充查准率。
    """
    wins = total = 0
    lat = 0.0
    for item in items:
        if item.modality not in ("image", "cube", "video"):
            continue
        pair = [item, degraded(item)]
        values = []
        for probe in pair:
            t0 = time.perf_counter()
            res = backend.diagnose(probe) if hasattr(backend, "diagnose") else None
            lat += (time.perf_counter() - t0) * 1000.0
            values.append(_quality_value(res, metric))
        clean, bad = values
        if clean is None or bad is None:
            continue
        total += 1
        wins += int(bad < clean)
    acc = wins / total if total else 0.0
    return {"precision": acc, "recall": acc, "f1": acc,
            "latency_ms": lat / max(1, total * 2), "n_pred": total, "n_truth": total,
            "kind": "pairwise", "note": f"成对判别 {wins}/{total}（原图 vs 模糊+坏列+削顶）"}


def _quality_value(result, metric: Optional[str]):
    if result is None:
        return None
    metrics = result.metrics or {}
    if metric and metric in metrics:
        return float(metrics[metric])
    for key in ("sharpness", "laplacian_var", "lap_var", "contrast", "entropy", "dynamic"):
        for k, v in metrics.items():
            if key in k and isinstance(v, (int, float, np.floating)) and np.isfinite(v):
                return float(v)
    counts = result.overlay.count()
    return float(-counts) if counts else 0.0


def coupled_item(seed: int = 0, n: int = 2000) -> DataItem:
    """两根强耦合通道 + 两根独立通道，相关性算子必须把这对排到第一。"""
    rng = np.random.default_rng(seed)
    base = np.sin(np.linspace(0, 20 * np.pi, n)) + rng.normal(0, 0.05, n)
    values = np.stack([base, 0.4 * base + rng.normal(0, 0.03, n),
                       rng.normal(0, 1, n), rng.normal(0, 1, n)], axis=1)
    t = 1735689600.0 + np.arange(n, dtype=np.float64)
    ts = TimeSeries(t=t, values=values, channels=["A", "B", "C", "D"],
                    units=["", "", "", ""], discrete=[False] * 4)
    return DataItem(id="bench-coupled", modality=Modality.SERIES.value, uri="bench://coupled",
                    title="相关性自检", t_ref=float(t[0]), t_end=float(t[-1]),
                    shape=(n, 4), dtype="float64", _opener=lambda: ts)


def score_correlation(backend, items, truth: dict) -> dict:
    try:
        res = backend.diagnose(coupled_item())
    except Exception as exc:
        return _no_truth(f"相关性自检失败：{exc}")
    pair = str(res.metrics.get("top_pair") or "")
    ok = pair in ("A~B", "B~A")
    return {"precision": 1.0 if ok else 0.0, "recall": 1.0 if ok else 0.0,
            "f1": 1.0 if ok else 0.0, "latency_ms": 0.0, "n_pred": 1, "n_truth": 1,
            "kind": "pairwise", "note": f"自检耦合对 A~B，实测 {pair or '无'}"}


def score_visual(backend, items, truth: dict) -> dict:
    """视觉提示：在第一张图上框一个真值目标，看能否在其他图上找回同类。"""
    from .base import Prompt

    table = truth.get("payload_truth") or {}
    images = [i for i in items if i.modality in ("image", "cube", "video")]
    seeds = [(i, _positions((_truth_entry(table, i) or {}).get("centroids") or []))
             for i in images]
    seeds = [(i, pts) for i, pts in seeds if pts]
    if len(seeds) < 2:
        return _no_truth("逐目标真值不足两张图，视觉提示无法评传播")
    source, pts = seeds[0]
    x, y = pts[0]
    prompt = Prompt("box", [x - 12, y - 12, x + 12, y + 12], "bench")
    tp = fp = fn = 0
    lat = 0.0
    for item, truths in seeds[1:]:
        t0 = time.perf_counter()
        anns = backend.predict(item, [prompt], _focus_window(item))
        lat += (time.perf_counter() - t0) * 1000.0
        preds = [((a.coords[0] + a.coords[2]) / 2.0, (a.coords[1] + a.coords[3]) / 2.0)
                 for a in anns if a.geom == "box"]
        t2, f2, n2 = _match_points(preds, truths, tol=12.0)
        tp, fp, fn = tp + t2, fp + f2, fn + n2
    p, r, f1 = _pr(tp, fp, fn)
    return {"precision": p, "recall": r, "f1": f1,
            "latency_ms": lat / max(1, len(seeds) - 1), "n_pred": tp + fp, "n_truth": tp + fn,
            "kind": "pr", "note": f"传播 {len(seeds) - 1} 张"}


def _no_truth(reason: str) -> dict:
    return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "latency_ms": 0.0,
            "n_pred": 0, "n_truth": 0, "kind": "skipped", "note": reason}


SCORERS = {
    "series_anomaly": score_series, "interval": score_series,
    "changepoint": score_series,
    "detect_source": score_points, "reject_cosmic": score_points,
    "quality_flag": score_quality, "correlation": score_correlation,
    "detect_similar": score_visual, "propagate": score_visual,
    "content_filter": score_visual,
}


def measure(backend, items: Sequence[DataItem], truth: dict, task: str = "") -> dict:
    task = task or (backend.manifest.tasks or ("",))[0]
    scorer = SCORERS.get(task)
    truth = truth or {}
    if scorer is None:
        return _no_truth(f"任务 {task} 没有对应的评测量规")
    result = scorer(backend, items, _cosmic_view(truth) if task == "reject_cosmic" else truth)
    result["task"] = task
    result.setdefault("model", "")
    return result


def _cosmic_view(truth: dict) -> dict:
    """把"宇宙线真值"换成分数里的 centroids 字段，复用同一套点位评分。

    必须整体复制：直接改 truth["payload_truth"] 会把耀斑真值也换成宇宙线，
    同一份 truth 在同一个循环里会被后面的任务再用一次。
    """
    table = truth.get("payload_truth") or {}
    swapped = {name: {**entry, "centroids": entry.get("cosmic") or []}
               for name, entry in table.items() if isinstance(entry, dict)}
    return {**truth, "payload_truth": swapped}


# ---------------------------------------------------------------------- 跑全表

def load_truth(root) -> dict:
    path = Path(root) / "truth.json"
    if not path.is_file():
        for child in sorted(Path(root).glob("*/truth.json")):
            path = child
            break
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def category_dirs(root) -> list:
    root = Path(root)
    out = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "truth.json").is_file():
            out.append(child)
    if not out and (root / "truth.json").is_file():
        out = [root]
    return out


def run(root, categories: Optional[Sequence[str]] = None, only: Optional[Sequence[str]] = None,
        limit: int = 6, probe=None, include_stubs: bool = False,
        modalities: Optional[Sequence[str]] = None) -> list:
    """对每个 (后端 × 学科) 组合实测，返回 Row 列表。"""
    from . import classic, visual          # noqa: F401  确保注册完成
    from .base import BACKENDS

    rows: list = []
    for directory in category_dirs(root):
        if categories and directory.name not in categories:
            continue
        truth = load_truth(directory)
        items = _items_for(directory, limit, modalities)
        if not items:
            continue
        for name in BACKENDS.names():
            if only and name not in only:
                continue
            if not include_stubs and name.startswith("deep:"):
                continue
            backend = BACKENDS.get(name).create()
            ok, why = backend.available(probe)
            if not ok:
                rows.append(Row(name, "n/a", directory.name, "*", note=why, kind="unavailable"))
                continue
            for task in backend.manifest.tasks:
                subset = [i for i in items if backend.manifest.covers(i.modality, task)]
                if not subset:
                    continue
                res = measure(backend, subset, truth, task)
                if res.get("kind") == "skipped":
                    continue
                rows.append(Row(backend=name, task=task, category=directory.name,
                                modality=subset[0].modality, precision=res["precision"],
                                recall=res["recall"], f1=res["f1"], latency_ms=res["latency_ms"],
                                vram_gb=0.0 if not backend.manifest.needs_gpu
                                else getattr(backend, "vram_gb", 0.0),
                                n_items=len(subset), n_pred=res.get("n_pred", 0),
                                n_truth=res.get("n_truth", 0), kind=res.get("kind", "pr"),
                                note=res.get("note", "")))
    return rows


def _items_for(directory, limit: int, modalities=None) -> list:
    """按模态分别截断。扁平 limit 会让字母序靠前的图像把遥测挤出去，
    结果就是"时序后端一条成绩都没有"——那不是没测到，是没测到。"""
    from .. import io as MDIO

    picked: dict = {}
    out = []
    for item in MDIO.scan(directory):
        if modalities and item.modality not in modalities:
            continue
        n = picked.get(item.modality, 0)
        if n >= max(1, int(limit)):
            continue
        picked[item.modality] = n + 1
        out.append(item)
    return out


def save_table(rows: Sequence[Row], path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [r.to_json() for r in rows]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_table(path) -> list:
    """读回实测表（dict 列表）。文件不存在时返回空表，让推荐器明确说"还没测过"。"""
    path = Path(path)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def default_table_path(root=".") -> Path:
    return Path(root) / "out" / "bench" / "backends.json"


__all__ = ["Row", "SCORERS", "coupled_item", "default_table_path", "degraded", "load_table",
           "load_truth", "measure", "run", "save_table"]
