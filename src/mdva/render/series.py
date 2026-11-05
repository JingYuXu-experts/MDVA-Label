"""L2 时序渲染：多通道时序 -> 位图。

这一层是"多模态"的关键适配器：把时序渲染成图像后，切片、放大、
网格拼接、导出这些原本只服务图像的能力可以原样复用，不必为每种
模态重做一套显示管线。
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np

from ..core.font import text_font
from ..core.model import Slice, TimeSeries
from . import downsample as ds
from ..core.timefmt import fmt_axis

_BG = (16, 18, 24)
_AXIS = (90, 98, 116)
_GRID = (38, 42, 54)
_TEXT = (176, 182, 196)
_SERIES = (110, 200, 255)
_CURSOR = (255, 210, 90)

MARGIN_L = 62
MARGIN_R = 8
MARGIN_T = 18
MARGIN_B = 22


def _nice_bounds(lo: float, hi: float, n_ticks: int = 4) -> Tuple[float, float, float]:
    """给出好看的坐标轴上下界与步长。"""
    if not (np.isfinite(lo) and np.isfinite(hi)):
        return (0.0, 1.0, 0.25)
    if hi <= lo:
        pad = abs(hi) * 0.05 or 1.0
        lo, hi = lo - pad, hi + pad
    raw = (hi - lo) / max(n_ticks, 1)
    mag = 10 ** np.floor(np.log10(abs(raw))) if raw else 1.0
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        step = mag * mult
        if step >= raw:
            break
    start = np.floor(lo / step) * step
    stop = np.ceil(hi / step) * step
    return (float(start), float(stop), float(step))


def render_channel(
    ts: TimeSeries,
    channel: int = 0,
    size: Tuple[int, int] = (900, 260),
    window: Optional[Slice] = None,
    method: str = "minmax",
    out_width: Optional[int] = None,
    ymin: Optional[float] = None,
    ymax: Optional[float] = None,
    discrete: Optional[bool] = None,
    cursor_t: Optional[float] = None,
    title: str = "",
) -> Tuple[np.ndarray, dict]:
    """渲染单通道，返回 (RGB, 轴信息)。轴信息供交互层做命中测试。"""
    from PIL import Image, ImageDraw

    width, height = int(size[0]), int(size[1])
    channel = int(np.clip(channel, 0, max(0, ts.n_channels - 1)))
    if discrete is None:
        discrete = ts.is_discrete(channel)
    sl = window or Slice.full()
    sub = ts.window(sl)
    plot_w = max(1, width - MARGIN_L - MARGIN_R)
    plot_h = max(1, height - MARGIN_T - MARGIN_B)

    canvas = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(canvas)

    if sub.t.size == 0:
        draw.text((MARGIN_L + 6, MARGIN_T + 6), "窗口内无数据", fill=_TEXT, font=text_font(12))
        return np.asarray(canvas), {}

    t0, t1 = float(sub.t[0]), float(sub.t[-1])
    if t1 <= t0:
        t1 = t0 + 1.0

    target = out_width or plot_w
    values = sub.values[:, channel]
    finite = values[np.isfinite(values)]

    if ymin is None or ymax is None:
        if finite.size:
            lo = float(np.percentile(finite, 0.5))
            hi = float(np.percentile(finite, 99.5))
            lo, hi = (lo, hi) if hi > lo else (lo - 1.0, hi + 1.0)
        else:
            lo, hi = 0.0, 1.0
        ymin, ymax, _ = _nice_bounds(lo, hi)

    def px(tv):
        return MARGIN_L + (tv - t0) / (t1 - t0) * plot_w

    def py(vv):
        v = np.clip(vv, ymin, ymax)
        return MARGIN_T + (ymax - v) / (ymax - ymin) * plot_h

    _draw_grid(draw, draw_x=(MARGIN_L, MARGIN_L + plot_w), draw_y=(MARGIN_T, MARGIN_T + plot_h))

    if discrete:
        xs = px(sub.t)
        ys = py(values)
        step_x, step_y = [], []
        for i in range(len(xs) - 1):
            step_x += [xs[i], xs[i + 1]]
            step_y += [ys[i], ys[i]]
        draw.line(list(zip(step_x, step_y)), fill=_SERIES, width=1)
        disp = {"x": xs, "y": ys}
    elif method == "lttb":
        sx, sy = ds.lttb(sub.t, values, target)
        draw.line(list(zip(px(sx), py(sy))), fill=_SERIES, width=1)
        disp = {"x": px(sx), "y": py(sy)}
    else:
        env = ds.minmax_envelope(values, target, t=sub.t)
        xs, lo_v, hi_v = env["x"], env["min"], env["max"]
        pxs = px(xs)
        seg = np.isfinite(lo_v) & np.isfinite(hi_v)
        # 包络用"从 max 到 min 的竖线段"表示，比填充多边形更省绘制且形状清晰
        for x, l, h in zip(pxs[seg], lo_v[seg], hi_v[seg]):
            draw.line([(x, py(h)), (x, py(l))], fill=_SERIES, width=1)
        mid = (lo_v + hi_v) / 2.0
        draw.line([(x, py(m)) for x, m in zip(pxs[seg], mid[seg])], fill=(180, 225, 255), width=1)
        disp = {"x": pxs, "y": py(mid)}

    _draw_axes(draw, width, height, t0, t1, ymin, ymax)

    if cursor_t is not None and t0 <= cursor_t <= t1:
        cx = px(cursor_t)
        draw.line([(cx, MARGIN_T), (cx, MARGIN_T + plot_h)], fill=_CURSOR, width=1)

    if title:
        draw.text((MARGIN_L + 4, 3), title[:70], fill=_TEXT, font=text_font(12))

    rgb = np.asarray(canvas)
    info = {
        "t0": t0, "t1": t1, "ymin": float(ymin), "ymax": float(ymax),
        "plot_box": (MARGIN_L, MARGIN_T, MARGIN_L + plot_w, MARGIN_T + plot_h),
        "n_samples": int(sub.t.size),
        "channel": int(channel),
    }
    return rgb, info


def _draw_grid(draw, draw_x, draw_y) -> None:
    x0, x1 = draw_x
    y0, y1 = draw_y
    for k in range(1, 5):
        y = y0 + (y1 - y0) * k / 5
        draw.line([(x0, y), (x1, y)], fill=_GRID, width=1)
    for k in range(1, 8):
        x = x0 + (x1 - x0) * k / 8
        draw.line([(x, y0), (x, y1)], fill=_GRID, width=1)


def _draw_axes(draw, width, height, t0, t1, ymin, ymax) -> None:
    x0, x1 = MARGIN_L, width - MARGIN_R
    y0, y1 = MARGIN_T, height - MARGIN_B
    draw.line([(x0, y0), (x0, y1), (x1, y1)], fill=_AXIS, width=1)
    font = text_font(10)

    lo, hi, step = _nice_bounds(ymin, ymax)
    v = lo
    while v <= hi + 1e-12:
        if ymin <= v <= ymax:
            y = y0 + (ymax - v) / (ymax - ymin) * (y1 - y0)
            draw.line([(x0 - 4, y), (x0, y)], fill=_AXIS, width=1)
            draw.text((4, y - 6), _fmt_num(v), fill=_TEXT, font=font)
        v += step

    for k in range(5):
        tv = t0 + (t1 - t0) * k / 4
        x = x0 + (x1 - x0) * k / 4
        draw.line([(x, y1), (x, y1 + 4)], fill=_AXIS, width=1)
        label = fmt_axis(tv, t1 - t0)
        # 按实测宽度居中：写死的 -34 偏移一换字体就偏到隔壁刻度下面
        _, _, lw, _ = draw.textbbox((0, 0), label, font=font)
        draw.text((x - lw / 2, y1 + 5), label, fill=_TEXT, font=font)


def _fmt_num(v: float) -> str:
    a = abs(v)
    if a == 0:
        return "0"
    if a >= 1e5 or a < 1e-3:
        return f"{v:.1e}"
    return f"{v:.4g}"
