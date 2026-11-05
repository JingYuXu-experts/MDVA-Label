"""时序类算子：滚动统计、自适应动态阈值、变点检测、多通道关联、孤立森林。

自适应动态阈值是这个课题里最贴近运控实际的一项：现网系统靠专家逐条设置
静态门限，参数一多就设不完，卫星性能随时间漂移后门限还会失效。这里用
滚动中位数 + MAD 生成随时间变化的上下包络，只需给一个 sigma 倍数。
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from ..core.model import DataItem, Slice
from ..core.registry import make_register_func
from ..render.overlay import OverlayData, VInterval
from .base import ANALYZERS, AnalyzeResult, Analyzer

register = make_register_func(ANALYZERS)


def _series(item: DataItem, window: Optional[Slice], channel: Optional[int]):
    ts = item.load()
    if window is not None:
        ts = ts.window(window)
    ch = 0 if channel is None else min(int(channel), ts.n_channels - 1)
    return ts, ch


def _merge_mask(mask: np.ndarray, min_run: int = 1) -> list:
    """把布尔序列合并成 [start, stop) 区间，短于 min_run 的丢弃。"""
    m = np.asarray(mask, dtype=bool)
    if not m.any():
        return []
    edges = np.flatnonzero(np.diff(np.concatenate(([0], m.view(np.int8), [0]))))
    out = []
    for s, e in zip(edges[::2], edges[1::2]):
        if e - s >= min_run:
            out.append((int(s), int(e)))
    return out


@register()
class RollingStats(Analyzer):
    name = "rolling_stats"
    modality = ("series",)
    description = "滚动均值/标准差/斜率"
    schema = {"window": {"type": "integer", "default": 60}, "channel": {"type": "integer", "default": 0}}

    def run(self, item: DataItem, window: Optional[Slice] = None) -> AnalyzeResult:
        ts, ch = _series(item, window, self.params.get("channel"))
        w = max(2, int(self.params.get("window") or 60))
        s = pd.Series(ts.values[:, ch])
        roll = s.rolling(w, center=True, min_periods=max(2, w // 4))
        mean = roll.mean().to_numpy()
        std = roll.std().to_numpy()

        res = AnalyzeResult(analyzer=self.name)
        res.metrics = {
            "mean": float(np.nanmean(mean)) if np.isfinite(mean).any() else 0.0,
            "std": float(np.nanmean(std)) if np.isfinite(std).any() else 0.0,
            "local_min": float(np.nanmin(mean)) if np.isfinite(mean).any() else 0.0,
            "local_max": float(np.nanmax(mean)) if np.isfinite(mean).any() else 0.0,
            "n": int(ts.t.size),
        }
        res.artifacts["rolling_mean"] = mean
        res.artifacts["rolling_std"] = std
        return res


@register()
class DynamicThreshold(Analyzer):
    """滚动中位数 ± k·MAD 包络。替代人工静态门限，随性能漂移自适应。"""

    name = "dynamic_threshold"
    modality = ("series",)
    description = "自适应动态阈值越界检测"
    schema = {
        "window": {"type": "integer", "default": 300},
        "nsigma": {"type": "number", "default": 5.0},
        "channel": {"type": "integer", "default": 0},
        "min_run": {"type": "integer", "default": 1},
    }

    def run(self, item: DataItem, window: Optional[Slice] = None) -> AnalyzeResult:
        ts, ch = _series(item, window, self.params.get("channel"))
        p = self.params
        w = max(4, int(p.get("window") or 300))
        k = float(p.get("nsigma") or 5.0)
        s = pd.Series(ts.values[:, ch])

        roll = s.rolling(w, center=True, min_periods=max(3, w // 5))
        base = roll.median()
        mad = (s - base).abs().rolling(w, center=True, min_periods=max(3, w // 5)).median()
        sigma = 1.4826 * mad
        sigma = sigma.replace(0.0, np.nan).ffill().bfill().fillna(float(s.std()) or 1.0)

        upper = (base + k * sigma).to_numpy()
        lower = (base - k * sigma).to_numpy()
        v = s.to_numpy()
        excursion = (v > upper) | (v < lower)
        excursion &= np.isfinite(v)

        overlay = OverlayData()
        flags = []
        for s_idx, e_idx in _merge_mask(excursion, int(p.get("min_run") or 1)):
            t0 = float(ts.t[s_idx])
            t1 = float(ts.t[min(e_idx, ts.t.size - 1)])
            seg = v[s_idx:e_idx]
            peak_dev = float(np.max(np.abs(seg - np.nanmean(base.to_numpy()[s_idx:e_idx]))))
            score = peak_dev / max(float(sigma.iloc[s_idx]), 1e-12)
            overlay.intervals.append(VInterval(t0=t0, t1=t1, level=2,
                                               label=f"越界 {t1 - t0:.1f}s"))
            flags.append(_flag(t0, ts.channels[ch], f"动态阈值越界", score,
                               (s_idx, e_idx)))

        res = AnalyzeResult(analyzer=self.name, overlay=overlay, flags=flags)
        res.metrics = {
            "n_events": len(flags),
            "excursion_fraction": float(excursion.mean()),
            "baseline": float(np.nanmedian(base)) if np.isfinite(base).any() else 0.0,
            "channel": ts.channels[ch],
        }
        res.artifacts["upper"] = upper
        res.artifacts["lower"] = lower
        res.artifacts["baseline"] = base.to_numpy()
        return res


@register()
class Changepoint(Analyzer):
    """二分分段法检测均值突变。对应姿态/电源分系统工作模式切换。"""

    name = "changepoint"
    modality = ("series",)
    description = "均值突变（二分分段）"
    schema = {"max_cp": {"type": "integer", "default": 10}, "threshold": {"type": "number", "default": 8.0},
              "channel": {"type": "integer", "default": 0}}

    def run(self, item: DataItem, window: Optional[Slice] = None) -> AnalyzeResult:
        ts, ch = _series(item, window, self.params.get("channel"))
        v = ts.values[:, ch]
        good = np.isfinite(v)
        idx = _binary_segmentation(v[good], ts.t[good],
                                   float(self.params.get("threshold") or 8.0),
                                   int(self.params.get("max_cp") or 10))

        overlay = OverlayData()
        flags = []
        for t, stat in idx:
            overlay.intervals.append(VInterval(t0=float(t), t1=float(t), level=3, label=f"变点 z={stat:.1f}"))
            flags.append(_flag(float(t), ts.channels[ch], "均值突变", float(stat), None))

        res = AnalyzeResult(analyzer=self.name, overlay=overlay, flags=flags)
        res.metrics = {"n_changepoints": len(idx), "channel": ts.channels[ch]}
        res.artifacts["changepoints"] = idx
        return res


def _binary_segmentation(v: np.ndarray, t: np.ndarray, threshold: float, max_cp: int) -> list:
    """递归找 t 统计量最大的分割点，超过阈值才接受。"""
    out = []

    def search(lo: int, hi: int):
        if hi - lo < 8 or len(out) >= max_cp:
            return
        seg = v[lo:hi]
        n = seg.size
        cands = np.arange(4, n - 4)
        if cands.size == 0:
            return
        left_mean = np.array([seg[:c].mean() for c in cands])
        right_mean = np.array([seg[c:].mean() for c in cands])
        # 用合并方差归一化，避免幅值大的通道垄断检测结果
        sd = np.array([np.sqrt((seg[:c].var() + seg[c:].var()) / 2.0) for c in cands])
        sd = np.where(sd > 1e-12, sd, 1.0)
        stat = np.abs(left_mean - right_mean) / sd * np.sqrt(cands * (n - cands) / n)
        k = int(np.argmax(stat))
        if stat[k] < threshold:
            return
        split = lo + int(cands[k])
        out.append((float(t[split]), float(stat[k])))
        search(lo, split)
        search(split, hi)

    search(0, v.size)
    out.sort(key=lambda x: x[0])
    return out


@register()
class ChannelCorrelation(Analyzer):
    """通道间相关矩阵。运控里定位根因靠的就是"哪几路参数一起变了"。"""

    name = "correlation"
    modality = ("series",)
    description = "多通道相关矩阵与最强耦合对"
    schema = {"top": {"type": "integer", "default": 5}, "min_valid": {"type": "integer", "default": 30}}

    def run(self, item: DataItem, window: Optional[Slice] = None) -> AnalyzeResult:
        ts, _ = _series(item, window, 0)
        p = self.params
        min_valid = int(p.get("min_valid") or 30)
        if ts.n_channels < 2 or ts.t.size < min_valid:
            res = AnalyzeResult(analyzer=self.name)
            res.metrics = {"n_channels": ts.n_channels, "usable": False}
            return res

        frame = pd.DataFrame(ts.values, columns=ts.channels)
        corr = frame.corr(min_periods=min_valid).to_numpy()
        np.fill_diagonal(corr, 0.0)

        order = np.argsort(np.nan_to_num(np.abs(corr).ravel()))[::-1]
        pairs = []
        for flat in order[: int(p.get("top") or 5) * 3]:
            i, j = divmod(int(flat), corr.shape[1])
            if i >= j:
                continue
            value = corr[i, j]
            if np.isfinite(value):
                pairs.append((ts.channels[i], ts.channels[j], float(value)))
            if len(pairs) >= int(p.get("top") or 5):
                break

        res = AnalyzeResult(analyzer=self.name)
        res.metrics = {
            "n_channels": ts.n_channels,
            "max_abs_corr": float(np.nanmax(np.abs(corr))) if np.isfinite(corr).any() else 0.0,
            "mean_abs_corr": float(np.nanmean(np.abs(corr))),
        }
        res.artifacts["corr"] = corr
        res.artifacts["pairs"] = pairs
        res.metrics["top_pair"] = f"{pairs[0][0]}~{pairs[0][1]}" if pairs else "-"
        return res


@register()
class IsolationForestDetect(Analyzer):
    """多变量无监督异常检测。缺 scikit-learn 时降级为马氏距离分位数。"""

    name = "iforest"
    modality = ("series",)
    description = "多通道无监督异常检测"
    schema = {"contamination": {"type": "number", "default": 0.02}, "window": {"type": "integer", "default": 60},
              "n_estimators": {"type": "integer", "default": 100}}

    def run(self, item: DataItem, window: Optional[Slice] = None) -> AnalyzeResult:
        ts, _ = _series(item, window, 0)
        p = self.params
        cont = float(p.get("contamination") or 0.02)

        x = ts.values
        keep = np.all(np.isfinite(x), axis=1)
        x = x[keep]
        t = ts.t[keep]
        if x.shape[0] < 20:
            res = AnalyzeResult(analyzer=self.name)
            res.metrics = {"usable": False, "n_samples": int(x.shape[0])}
            return res

        z = (x - x.mean(0)) / np.where(x.std(0) > 1e-12, x.std(0), 1.0)
        try:
            from sklearn.ensemble import IsolationForest

            model = IsolationForest(
                n_estimators=int(p.get("n_estimators") or 100),
                contamination=cont, random_state=0,
            )
            pred = model.fit_predict(z)
            scores = -model.score_samples(z)
        except ImportError:
            d = np.sqrt(np.sum(z ** 2, axis=1))
            cut = np.quantile(d, 1.0 - cont)
            pred = np.where(d > cut, -1, 1)
            scores = d

        anomalies = pred < 0
        overlay = OverlayData()
        flags = []
        for s_idx, e_idx in _merge_mask(anomalies):
            t0 = float(t[s_idx])
            t1 = float(t[min(e_idx, t.size - 1)])
            peak = float(scores[s_idx:e_idx].max())
            overlay.intervals.append(VInterval(t0=t0, t1=t1, level=3, label=f"异常 {e_idx - s_idx}点"))
            flags.append(_flag(t0, "multi", "无监督异常", peak, (s_idx, e_idx)))

        res = AnalyzeResult(analyzer=self.name, overlay=overlay, flags=flags)
        res.metrics = {
            "n_anomalies": int(anomalies.sum()),
            "rate": float(anomalies.mean()),
            "max_score": float(scores.max()) if scores.size else 0.0,
            "n_samples": int(x.shape[0]),
        }
        res.artifacts["scores"] = scores
        res.artifacts["anomaly_time"] = t[anomalies]
        return res


def _flag(t: float, channel: str, label: str, score: float, span) -> "Flag":
    from .base import Flag

    return Flag(t=float(t), channel=str(channel), label=label, score=float(score),
                box=None if span is None else tuple(span))
