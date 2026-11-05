"""L2 渲染层：位深归一化 -> 区间估计 -> 灰度拉伸 -> 伪彩映射。

科学图像与照片的根本差别在于动态范围：一张 X 射线计数图的值域可能跨
3~4 个数量级且绝大多数像元是背景，用 8 位照片的 min-max 直接映射会把
暗弱的真实结构压成一片黑。因此这里把"区间估计"和"拉伸函数"拆开，
让两者可以自由组合，并保留 astropy 的天文标准实现作为首选。
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

import numpy as np

try:
    from astropy.visualization import ZScaleInterval
    _HAS_ASTROPY = True
except ImportError:  # astropy 缺失时走纯 numpy 分支，功能降级但可用
    _HAS_ASTROPY = False

INTERVALS = ("minmax", "percent", "sigma", "zscale", "median")
STRETCHES = ("linear", "sqrt", "power", "gamma", "log", "asinh", "histeq", "clahe")

_BAD = np.nan


def clean(arr: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """返回 (原数组, 有限值掩膜)。NaN/Inf 视为坏像元，不参与统计也不渲染。"""
    a = np.asarray(arr, dtype=np.float64)
    good = np.isfinite(a)
    return a, good


def _sample(values: np.ndarray, max_pixels: int = 100_000, seed: int = 0) -> np.ndarray:
    """等距抽样而非随机抽样：保证同图多次拉伸结果可复现。"""
    if values.size <= max_pixels:
        return values
    step = math.ceil(values.size / max_pixels)
    return values[::step]


def _ordered(lo: float, hi: float) -> Tuple[float, float]:
    """保证 hi > lo：常数图或极端分位数会得到相等上下界，直接用于归一会除零。"""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return (0.0, 1.0)
    if hi <= lo:
        span = abs(hi) * 0.05 or 1.0
        return (lo - span / 2, hi + span / 2)
    return (lo, hi)


def estimate_interval(
    arr: np.ndarray,
    method: str = "zscale",
    percent: float = 99.9,
    nsigma: float = 3.0,
    max_pixels: int = 100_000,
) -> Tuple[float, float]:
    """估计显示区间。返回值恒满足 hi > lo，调用方可直接用于归一化。"""
    a, good = clean(arr)
    vals = _sample(a[good], max_pixels)
    if vals.size < 5:
        return (0.0, 1.0)

    if method == "minmax":
        return _ordered(float(vals.min()), float(vals.max()))

    if method == "percent":
        return _ordered(float(np.percentile(vals, 100 - percent)), float(np.percentile(vals, percent)))

    if method == "sigma":
        med = float(np.median(vals))
        # MAD 比标准差稳健，不会被源本身的高值带偏
        mad = float(np.median(np.abs(vals - med))) or 1e-12
        sigma = 1.4826 * mad
        return _ordered(med - nsigma * sigma, med + nsigma * sigma)

    if method == "median":
        med = float(np.median(vals))
        spread = float(np.percentile(vals, 99.9) - med) or 1.0
        return _ordered(med - spread, med + spread)

    if method == "zscale":
        if _HAS_ASTROPY:
            # 输入已被 _sample 限幅，这里不再向 astropy 传采样上限
            lo, hi = ZScaleInterval().get_limits(vals)
            return _ordered(float(lo), float(hi))
        return _ordered(*_zscale_fallback(vals))

    raise ValueError(f"未知区间方法: {method}（可用 {INTERVALS}）")


def _zscale_fallback(vals: np.ndarray) -> Tuple[float, float]:
    """无 astropy 时的近似 zscale：中位数 + 上下四分位斜率外推。

    只用于降级运行，不能替代 astropy 的完整迭代剔除版本。
    """
    ordered = np.sort(vals)
    n = ordered.size
    median = float(ordered[n // 2])
    q25 = float(ordered[n // 4])
    q75 = float(ordered[(3 * n) // 4])
    slope = (q75 - q25) / max(1.0, (n * 0.5))
    span = max(slope * n * 0.125, 1e-12)
    return (median - span, median + span)


def stretch_linear(norm: np.ndarray, gamma: float = 1.0) -> np.ndarray:
    return np.power(np.clip(norm, 0.0, 1.0), 1.0 / max(gamma, 1e-6))


def stretch_log(norm: np.ndarray, offset: float = 1.0) -> np.ndarray:
    x = np.clip(norm, 0.0, 1.0)
    return np.log1p(x * offset) / np.log1p(offset) if offset > 1e-9 else x


def stretch_asinh(norm: np.ndarray, softening: float = 100.0) -> np.ndarray:
    """DS9 风格 asinh：a = arcsinhstretch/9.9999，结果归一到 [0,1]。"""
    x = np.clip(norm, 0.0, 1.0)
    a = max(softening, 1e-6) / 9.9999
    denom = math.asinh(1.0 / a)
    return np.arcsinh(x / a) / denom


def stretch_histeq(norm: np.ndarray, bins: int = 256) -> np.ndarray:
    x = np.clip(norm, 0.0, 1.0)
    hist, edges = np.histogram(x, bins=bins, range=(0.0, 1.0))
    cdf = hist.cumsum(dtype=np.float64)
    if cdf[-1] == 0:
        return x
    cdf = (cdf - cdf[0]) / (cdf[-1] - cdf[0])
    pos = np.clip((x * bins).astype(np.int32), 0, bins - 1)
    return cdf[pos]


def stretch_clahe(norm: np.ndarray, clip: float = 3.0, grid: int = 8) -> np.ndarray:
    """对比度受限自适应直方图均衡。缺 scikit-image 时降级为全局直方图均衡。"""
    x = np.clip(norm, 0.0, 1.0)
    try:
        from skimage.exposure import equalize_adapthist

        return equalize_adapthist(x, kernel_size=grid, clip_limit=clip, nbins=256)
    except ImportError:
        return stretch_histeq(x)


def normalize(
    arr: np.ndarray,
    interval: str = "zscale",
    stretch: str = "linear",
    vmin: Optional[float] = None,
    vmax: Optional[float] = None,
    gamma: float = 1.0,
    softening: float = 100.0,
    percent: float = 99.9,
    nsigma: float = 3.0,
    invert: bool = False,
    bad: float = 0.0,
) -> Tuple[np.ndarray, dict]:
    """任意位深数组 -> [0,1] 浮点显示图，同时返回所用区间供界面显示与复现。"""
    a, good = clean(arr)
    if vmin is None or vmax is None:
        lo, hi = estimate_interval(a, interval, percent=percent, nsigma=nsigma)
    else:
        lo, hi = float(vmin), float(vmax)
    lo, hi = _ordered(lo, hi)

    x = np.clip((a - lo) / (hi - lo), 0.0, 1.0)

    _FIXED_GAMMA = {"linear": 1.0, "sqrt": 0.5}
    if stretch in _FIXED_GAMMA:
        y = stretch_linear(x, _FIXED_GAMMA[stretch])
    elif stretch in ("power", "gamma"):
        y = stretch_linear(x, gamma)
    elif stretch == "log":
        y = stretch_log(x, softening)
    elif stretch == "asinh":
        y = stretch_asinh(x, softening)
    elif stretch == "histeq":
        y = stretch_histeq(x)
    elif stretch == "clahe":
        y = stretch_clahe(x)
    else:
        raise ValueError(f"未知拉伸方式: {stretch}（可用 {STRETCHES}）")

    if invert:
        y = 1.0 - y
    y = np.where(good, y, bad)

    info = {
        "vmin": float(lo),
        "vmax": float(hi),
        "interval": interval,
        "stretch": stretch,
        "bad_pixels": int((~good).sum()),
        "dynamic_range": float(np.nanmax(a[good]) - np.nanmin(a[good])) if good.any() else 0.0,
    }
    return np.clip(y, 0.0, 1.0), info


def _build(name: str) -> np.ndarray:
    """内置伪彩 LUT，避免依赖 matplotlib。"""
    n = 256
    t = np.linspace(0.0, 1.0, n)
    if name == "gray":
        return np.repeat((t * 255)[:, None], 3, axis=1).astype(np.uint8)
    tables = {
        # viridis / inferno / plasma 的控制点取自 matplotlib 公开色图端点采样
        "viridis": [(68, 1, 84), (59, 82, 139), (33, 145, 140), (94, 201, 98), (253, 231, 37)],
        "inferno": [(0, 0, 4), (87, 16, 110), (188, 55, 84), (249, 142, 9), (252, 255, 164)],
        "plasma": [(13, 8, 135), (126, 3, 168), (204, 71, 120), (248, 149, 64), (240, 249, 33)],
        "hot": [(0, 0, 0), (234, 0, 0), (255, 255, 0), (255, 255, 255)],
        "cool": [(0, 255, 255), (255, 0, 255)],
    }
    pts = np.array(tables[name], dtype=np.float64)
    pos = np.linspace(0.0, 1.0, len(pts))
    out = np.empty((n, 3), dtype=np.uint8)
    for ch in range(3):
        out[:, ch] = np.interp(t, pos, pts[:, ch])
    return out


_LUT_CACHE: dict = {}


def colormap_lut(name: str) -> np.ndarray:
    if name not in _LUT_CACHE:
        _LUT_CACHE[name] = _build(name)
    return _LUT_CACHE[name]


def colormap_names() -> list:
    return ["gray", "viridis", "inferno", "plasma", "hot", "cool"]


def apply_colormap(norm: np.ndarray, name: str = "gray") -> np.ndarray:
    """[0,1] 浮点 -> uint8 RGB。(n,3) LUT 索引查找。"""
    lut = colormap_lut(name)
    idx = np.clip(norm * 255.0, 0, 255).astype(np.uint8)
    return lut[idx]


def to_rgb(
    arr: np.ndarray,
    interval: str = "zscale",
    stretch: str = "linear",
    colormap: str = "gray",
    **kwargs,
) -> Tuple[np.ndarray, dict]:
    """一步到位：科学数组 -> (H,W,3) uint8 + 渲染参数记录。"""
    norm, info = normalize(arr, interval=interval, stretch=stretch, **kwargs)
    info["colormap"] = colormap
    return apply_colormap(norm, colormap), info
