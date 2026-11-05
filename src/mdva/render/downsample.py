"""L2 时序降采样。

运控值班场景里，一条 1Hz 遥测曲线一天有 86400 点，几十路通道同时显示就
超过千万点，必须降采样到与屏幕像素同量级。但遥测的价值恰恰在尖峰：一次
几十毫秒的电压毛刺可能就是异常先兆，被平均掉等于漏检。

因此这里提供两类语义不同的降采样，并把"异常保留率"作为一等指标：
- minmax 包络：每个像素列保留最小/最大值，尖峰必然存活，但形态会变粗
- LTTB：保持视觉形态，代价是可能整段跳过窄尖峰
两者不能互相替代，所以渲染层默认用包络，形态分析用 LTTB。
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

import numpy as np


def minmax_envelope(
    values: np.ndarray,
    out_width: int,
    t: Optional[np.ndarray] = None,
) -> dict:
    """等分箱的 min/max 包络。全向量化，1e6 点在毫秒级。

    返回 {"x": (W,) 箱中心, "min": (W,), "max": (W,), "box": (W,2) 样本区间}
    NaN 被忽略；整箱全 NaN 时该箱输出 NaN，渲染层据此断开连线。
    """
    v = np.asarray(values, dtype=np.float64).ravel()
    n = v.size
    if n == 0 or out_width <= 0:
        empty = np.full(0, np.nan)
        return {"x": empty, "min": empty, "max": empty, "box": np.zeros((0, 2), dtype=np.int64), "t": empty}

    out_width = min(out_width, n)
    edges = np.linspace(0, n, out_width + 1).astype(np.int64)
    starts = edges[:-1]
    box = np.stack([starts, edges[1:]], axis=1)

    # reduceat 会把 NaN 传染给整个分箱，因此用 ±inf 占位再单独还原空箱
    v_min = np.where(np.isfinite(v), v, np.inf)
    v_max = np.where(np.isfinite(v), v, -np.inf)
    mins = np.minimum.reduceat(v_min, starts)
    maxs = np.maximum.reduceat(v_max, starts)
    mins = np.where(np.isposinf(mins), np.nan, mins)
    maxs = np.where(np.isneginf(maxs), np.nan, maxs)

    if t is not None:
        tt = np.asarray(t, dtype=np.float64).ravel()
        x = tt[np.clip(starts, 0, tt.size - 1)]
    else:
        x = box.mean(axis=1).astype(np.float64)

    return {"x": x, "min": mins, "max": maxs, "box": box, "t": x}


def lttb(
    t: np.ndarray,
    values: np.ndarray,
    out_width: int,
) -> Tuple[np.ndarray, np.ndarray]:
    """Largest-Triangle-Three-Buckets，Steinarsson 2013 的向量化实现。

    首尾点强制保留，中间按桶挑使三角形面积最大的点。
    """
    tv = np.asarray(t, dtype=np.float64).ravel()
    v = np.asarray(values, dtype=np.float64).ravel()
    n = v.size
    if n <= out_width or out_width < 3:
        return tv, v

    idx = np.empty(out_width, dtype=np.int64)
    idx[0] = 0
    idx[-1] = n - 1

    bucket = (n - 2) / (out_width - 2)
    # 用有限值均值构造下一桶的重心；整桶无有效值时退化为桶中心
    a_x, a_y = tv[0], v[0]

    for i in range(1, out_width - 1):
        r0 = int(math.floor((i - 1) * bucket)) + 1
        r1 = int(math.floor(i * bucket)) + 1
        n0 = int(math.floor(i * bucket)) + 1
        n1 = int(math.floor((i + 1) * bucket)) + 1
        r1 = min(r1, n)
        n1 = min(n1, n)

        nxt_t, nxt_v = tv[n0:n1], v[n0:n1]
        ok = np.isfinite(nxt_v)
        if ok.any():
            avg_x = float(nxt_t[ok].mean())
            avg_y = float(nxt_v[ok].mean())
        else:
            avg_x = float(nxt_t.mean()) if nxt_t.size else float(tv[r1 - 1])
            avg_y = 0.0

        cand_t, cand_v = tv[r0:r1], v[r0:r1]
        if cand_t.size == 0:
            idx[i] = min(r1 - 1, n - 1)
            continue

        valid = np.isfinite(cand_v)
        if not valid.any():
            idx[i] = int(np.argmax(cand_t)) + r0
            continue

        # 三角形面积（未除 2），与 a 点和下一桶重心构成的叉积绝对值
        area = np.abs(
            (avg_x - a_x) * (cand_v - a_y) - (avg_x - cand_t) * (a_y - avg_y)
        )
        area = np.where(valid, area, -np.inf)
        pick = int(np.nanargmax(area))
        idx[i] = r0 + pick
        a_x, a_y = float(cand_t[pick]), float(cand_v[pick])

    return tv[idx], v[idx]


def lod_levels(n_samples: int, base_width: int = 1024, factor: int = 8) -> list:
    """预计算可用的细节层级（点数上限）。"""
    levels = []
    width = base_width
    while width < n_samples:
        levels.append(width)
        width *= factor
    levels.append(n_samples)
    return levels


class LodPyramid:
    """把同一通道预先降采样成多个层级，缩放时按当前窗口宽度选层。

    避免每次拖动都重算百万点的 min/max。
    """

    def __init__(self, t: np.ndarray, values: np.ndarray, levels: Optional[list] = None, base_width: int = 1024):
        self.t = np.asarray(t, dtype=np.float64).ravel()
        self.v = np.asarray(values, dtype=np.float64).ravel()
        self.widths = levels or lod_levels(self.v.size, base_width)
        self._cache = {}

    def level(self, out_width: int) -> dict:
        for w in self.widths:
            if w >= out_width:
                return self._at(w)
        return self._at(self.widths[-1])

    def _at(self, width: int) -> dict:
        if width not in self._cache:
            self._cache[width] = minmax_envelope(self.v, width, t=self.t)
        return self._cache[width]

    @property
    def memory_bytes(self) -> int:
        return sum(arr.nbytes for d in self._cache.values() for arr in d.values() if isinstance(arr, np.ndarray))


def envelope_peaks(env: dict) -> np.ndarray:
    """每箱偏离零最远的那个极值（保留符号）。

    保真度评测要用这个而不是 env["max"]：负向尖峰只存在于 min 分支，
    单取 max 会把负异常判成"被抹平"。
    """
    lo, hi = env["min"], env["max"]
    both_bad = ~np.isfinite(lo) & ~np.isfinite(hi)
    a = np.where(np.isfinite(lo), np.abs(lo), 0.0)
    b = np.where(np.isfinite(hi), np.abs(hi), 0.0)
    out = np.where(b >= a, np.where(np.isfinite(hi), hi, 0.0), np.where(np.isfinite(lo), lo, 0.0))
    return np.where(both_bad, np.nan, out)


def anomaly_retention(
    original: np.ndarray,
    shown: np.ndarray,
    box,
    anomaly_idx,
    keep_ratio: float = 0.5,
) -> float:
    """降采样保真度：异常幅值在降采样结果里还剩多少。

    判据是"箱内显示值达到原峰值的比例"，而不是"与原值有差异"——后者对
    箱均值也成立（均值当然和原值不同），会把最差的方法判成满分。
    """
    orig = np.asarray(original, dtype=np.float64).ravel()
    disp = np.asarray(shown, dtype=np.float64).ravel()
    edges = np.asarray(box, dtype=np.int64)
    idx = np.asarray(list(anomaly_idx), dtype=np.int64)
    if orig.size == 0 or idx.size == 0 or edges.shape[0] != disp.size:
        return float("nan")

    # 分箱连续覆盖 [0, n)，用箱起点反查异常点落在哪一箱
    bins = np.searchsorted(edges[:, 0], idx, side="right") - 1
    retained = 0
    counted = 0
    for j, b in zip(idx, bins):
        if b < 0 or b >= disp.size:
            continue
        peak = abs(float(orig[j]))
        if peak <= 0:
            continue
        counted += 1
        value = disp[b]
        if np.isfinite(value) and abs(float(value)) >= keep_ratio * peak:
            retained += 1
    return retained / counted if counted else float("nan")


def detect_anomaly_indices(values: np.ndarray, nsigma: float = 5.0) -> np.ndarray:
    """用稳健 z 分数标出尖峰位置，作为保留率实验的标注来源。"""
    v = np.asarray(values, dtype=np.float64).ravel()
    finite = v[np.isfinite(v)]
    if finite.size == 0:
        return np.array([], dtype=np.int64)
    med = np.median(finite)
    mad = np.median(np.abs(finite - med)) or 1e-12
    z = np.abs(v - med) / (1.4826 * mad)
    return np.flatnonzero(np.nan_to_num(z, nan=0.0) > nsigma)
