"""图像类算子：背景统计、点源检测与亚像元质心、宇宙线/坏像元剔除、质量评估。

这些都是空间科学载荷数据处理的标准环节，也是图像分析能被量化评测的原因：
合成数据下点源位置真值已知，可以直接算检出率与质心误差。
"""

from __future__ import annotations

from typing import Optional

import numpy as np
from scipy import ndimage

from ..core.model import DataItem, Slice
from ..core.registry import make_register_func
from ..render.image import pick_slice
from ..render.overlay import Box, OverlayData, Point
from .base import ANALYZERS, AnalyzeResult, Analyzer, to_gray

register = make_register_func(ANALYZERS)


def sigma_clipped_stats(arr: np.ndarray, sigma: float = 3.0, maxiters: int = 5) -> tuple:
    """稳健背景均值/中位/标准差。优先用 astropy，缺失时用 MAD 迭代。"""
    a = np.asarray(arr, dtype=np.float64).ravel()
    a = a[np.isfinite(a)]
    if a.size == 0:
        return (0.0, 0.0, 0.0)
    try:
        from astropy.stats import sigma_clipped_stats as _acs

        mean, median, std = _acs(a, sigma=sigma, maxiters=maxiters)
        return (float(mean), float(np.median(a)), float(std))
    except ImportError:
        pass
    keep = a
    for _ in range(maxiters):
        med = np.median(keep)
        mad = np.median(np.abs(keep - med)) or 1e-12
        gate = sigma * 1.4826 * mad
        mask = np.abs(keep - med) <= gate
        if mask.all() or mask.sum() < 8:
            break
        keep = keep[mask]
    return (float(keep.mean()), float(np.median(keep)), float(keep.std()))


@register()
class BackgroundStats(Analyzer):
    name = "bg_stats"
    modality = ("image", "cube", "video")
    description = "稳健背景统计（sigma 迭代剔除）"
    schema = {"sigma": {"type": "number", "default": 3.0}}

    def run(self, item: DataItem, window: Optional[Slice] = None) -> AnalyzeResult:
        arr = to_gray(pick_slice(item, window or Slice.full()))
        mean, median, std = sigma_clipped_stats(arr, sigma=float(self.params.get("sigma") or 3.0))
        finite = arr[np.isfinite(arr)]
        res = AnalyzeResult(analyzer=self.name)
        res.metrics = {
            "bg_mean": mean,
            "bg_median": median,
            "bg_sigma": std,
            "peak": float(finite.max()) if finite.size else 0.0,
            "dynamic_range": float(finite.max() - median) if finite.size else 0.0,
            "n_valid": int(finite.size),
            "n_bad": int(arr.size - finite.size),
        }
        return res


@register()
class StarDetect(Analyzer):
    """局部极大值 + 稳健阈值筛点源，再用对数抛物线拟合求亚像元中心。"""

    name = "star_detect"
    modality = ("image", "cube", "video")
    description = "点源检测与亚像元质心"
    schema = {
        "nsigma": {"type": "number", "default": 5.0},
        "footprint": {"type": "integer", "default": 5},
        "min_sep": {"type": "integer", "default": 4},
        "max_sources": {"type": "integer", "default": 200},
    }

    def run(self, item: DataItem, window: Optional[Slice] = None) -> AnalyzeResult:
        arr = to_gray(np.nan_to_num(pick_slice(item, window or Slice.full()),
                                  nan=0.0, posinf=0.0, neginf=0.0))
        p = self.params
        nsigma = float(p.get("nsigma") or 5.0)
        fp = max(3, int(p.get("footprint") or 5))
        half = max(3, fp // 2)

        mean, median, std = sigma_clipped_stats(arr)
        thresh = median + nsigma * std

        local_max = arr >= ndimage.maximum_filter(arr, size=fp, mode="nearest")
        candidate = local_max & (arr > thresh)

        ys, xs = np.nonzero(candidate)
        if xs.size == 0:
            res = AnalyzeResult(analyzer=self.name)
            res.metrics = {"n_sources": 0, "threshold": float(thresh), "bg_sigma": float(std)}
            return res

        order = np.argsort(arr[ys, xs])[::-1]
        ys, xs = ys[order], xs[order]
        ys, xs = _dedupe_near(ys, xs, int(p.get("min_sep") or 4))
        limit = int(p.get("max_sources") or 200)
        ys, xs = ys[:limit], xs[:limit]

        overlay = OverlayData()
        centroids = []
        for y, x in zip(ys, xs):
            cy, cx, flux = _centroid(arr, int(y), int(x))
            centroids.append((cx, cy, float(arr[y, x]), flux))
            overlay.points.append(Point(x=cx, y=cy, label=f"{flux:.3g}", radius=4))
            overlay.boxes.append(Box(x0=cx - half, y0=cy - half, x1=cx + half, y1=cy + half))

        res = AnalyzeResult(analyzer=self.name, overlay=overlay)
        res.metrics = {
            "n_sources": len(centroids),
            "threshold": float(thresh),
            "brightest": float(max((c[2] for c in centroids), default=0.0)),
            "bg_sigma": float(std),
        }
        res.artifacts["centroids"] = centroids
        return res


def _dedupe_near(ys, xs, min_sep: int):
    """按亮度降序贪心去重，避免一个亮源周围堆一圈假源。"""
    keep_y, keep_x = [], []
    r2 = min_sep * min_sep
    for y, x in zip(ys, xs):
        if keep_y:
            dy = np.asarray(keep_y) - y
            dx = np.asarray(keep_x) - x
            if np.any(dy * dy + dx * dx < r2):
                continue
        keep_y.append(y)
        keep_x.append(x)
    return np.asarray(keep_y, dtype=np.int64), np.asarray(keep_x, dtype=np.int64)


def _centroid(arr: np.ndarray, y: int, x: int, box: int = 2) -> tuple:
    """窗口质心 + 对数抛物线亚像元修正。

    高斯型点源取对数后是抛物线，三点拟合给出的中心不受背景常数影响，
    比强度加权质心更准；信号非正或修正超界时退回质心结果。
    """
    h, w = arr.shape
    y0, y1 = max(0, y - box), min(h, y + box + 1)
    x0, x1 = max(0, x - box), min(w, x + box + 1)
    sub = np.clip(arr[y0:y1, x0:x1].astype(np.float64), 0.0, None)
    total = float(sub.sum())
    if total <= 0:
        return float(y), float(x), 0.0
    yy, xx = np.mgrid[y0:y1, x0:x1]
    cy = float((sub * yy).sum() / total)
    cx = float((sub * xx).sum() / total)

    tiny = np.finfo(float).tiny
    if 1 <= x < w - 1 and 1 <= y < h - 1:
        lm, l0, rp = np.log(np.maximum([arr[y, x - 1], arr[y, x], arr[y, x + 1]], tiny))
        denom = lm - 2 * l0 + rp
        if abs(denom) > 1e-12:
            dx = 0.5 * (lm - rp) / denom
            if abs(dx) <= 1.0:
                cx = x + dx
        um, u0, dn = np.log(np.maximum([arr[y - 1, x], arr[y, x], arr[y + 1, x]], tiny))
        denom = um - 2 * u0 + dn
        if abs(denom) > 1e-12:
            dy = 0.5 * (um - dn) / denom
            if abs(dy) <= 1.0:
                cy = y + dy
    return cy, cx, total


@register()
class CosmicReject(Analyzer):
    """单像元级锐利偏离。真点源跨多个像元，宇宙线旁邻是背景，用局部展宽比区分。"""

    name = "cosmic_reject"
    modality = ("image", "cube", "video")
    description = "宇宙线/坏像元剔除（中值滤波残差）"
    schema = {"nsigma": {"type": "number", "default": 6.0}, "size": {"type": "integer", "default": 3}}

    def run(self, item: DataItem, window: Optional[Slice] = None) -> AnalyzeResult:
        a = to_gray(np.nan_to_num(pick_slice(item, window or Slice.full()),
                              nan=0.0, posinf=0.0, neginf=0.0))
        nsigma = float(self.params.get("nsigma") or 6.0)
        ksize = max(3, int(self.params.get("size") or 3))

        resid = a - ndimage.median_filter(a, size=ksize)
        _, _, std = sigma_clipped_stats(resid)
        spike = resid > nsigma * max(std, 1e-12)
        spread = ndimage.uniform_filter(a, size=3)
        isolated = spike & (spread < 0.4 * a)

        ys, xs = np.nonzero(isolated)
        overlay = OverlayData()
        for y, x in zip(ys[:400], xs[:400]):
            overlay.points.append(Point(x=float(x), y=float(y), radius=2, color=(255, 80, 80)))

        res = AnalyzeResult(analyzer=self.name, overlay=overlay)
        res.metrics = {
            "n_cosmic": int(isolated.sum()),
            "resid_sigma": float(std),
            "fraction": float(isolated.mean()),
        }
        res.artifacts["mask"] = isolated
        return res


@register()
class ImageQuality(Analyzer):
    name = "image_quality"
    modality = ("image", "cube", "video")
    description = "调焦度、信噪比、饱和与坏像元比例"
    schema = {"saturation": {"type": "number", "default": None}}

    def run(self, item: DataItem, window: Optional[Slice] = None) -> AnalyzeResult:
        a = to_gray(pick_slice(item, window or Slice.full()))
        good = np.isfinite(a)
        g = a[good]
        _, median, std = sigma_clipped_stats(a)

        gy, gx = np.gradient(np.nan_to_num(a))
        sharpness = float(np.mean(gx * gx + gy * gy)) if a.size else 0.0

        # 高频能量占比：模糊图能量集中在低频，比值随散焦下降
        # rfft2 输出是 (h, w//2+1) 且 y 轴未 fftshift，必须用 fftfreq 还原真实频率坐标
        clean = np.nan_to_num(a) - median
        fft = np.abs(np.fft.rfft2(clean))
        h, w = fft.shape[0], a.shape[1]
        fy = np.fft.fftfreq(h)[:, None]
        fx = np.fft.rfftfreq(w)[None, :]
        norm = float(np.hypot(0.5, 0.5)) or 1.0
        radius = np.hypot(fy, fx) / norm
        total = float(fft.sum()) or 1.0
        hf = float(fft[radius > 0.5].sum()) / total

        sat = self.params.get("saturation")
        saturated = float(np.mean(g >= float(sat))) if sat is not None and g.size else 0.0

        res = AnalyzeResult(analyzer=self.name)
        res.metrics = {
            "snr": float((g.max() - median) / std) if std > 0 and g.size else 0.0,
            "sharpness": sharpness,
            "hf_ratio": hf,
            "contrast_std": float(g.std()) if g.size else 0.0,
            "bad_fraction": float(1.0 - good.mean()),
            "saturated_fraction": saturated,
        }
        return res
