"""L2 叠加层：把分析结果画到已渲染的位图上。

算子输出与像素绘制解耦——算子只产生几何标注，由这里统一落图。
这样同一份分析结果可以画到桌面位图、Web SVG 或导出报告，
也是"可视化与检测闭环"这一设计的实现位置。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

DEFAULT_COLORS = ((255, 80, 80), (80, 200, 255), (255, 200, 60), (120, 255, 120), (220, 120, 255))


@dataclass
class Box:
    x0: float
    y0: float
    x1: float
    y1: float
    label: str = ""
    color: Optional[tuple] = None
    score: float = 1.0


@dataclass
class Point:
    x: float
    y: float
    label: str = ""
    color: Optional[tuple] = None
    radius: int = 3
    cross: bool = True


@dataclass
class Polyline:
    pts: Sequence
    label: str = ""
    color: Optional[tuple] = None


@dataclass
class VInterval:
    """时间轴上的区间标注，供时序图与事件轴共用。"""
    t0: float
    t1: float
    label: str = ""
    color: Optional[tuple] = None
    level: int = 0


@dataclass
class OverlayData:
    boxes: list = field(default_factory=list)
    points: list = field(default_factory=list)
    polylines: list = field(default_factory=list)
    intervals: list = field(default_factory=list)
    text: list = field(default_factory=list)

    def extend(self, other: "OverlayData") -> "OverlayData":
        for name in ("boxes", "points", "polylines", "intervals", "text"):
            getattr(self, name).extend(getattr(other, name))
        return self

    def is_empty(self) -> bool:
        return not any(getattr(self, n) for n in ("boxes", "points", "polylines", "intervals", "text"))

    def count(self) -> int:
        return sum(len(getattr(self, n)) for n in ("boxes", "points", "polylines", "intervals", "text"))


def _color_of(obj, i: int) -> tuple:
    return obj.color or DEFAULT_COLORS[i % len(DEFAULT_COLORS)]


class LabelPlacer:
    """栅格占用式标签避让。

    一张 512² 的场里可能有上百个点源，逐个标文字会糊成一片。算子已按
    亮度降序输出标注，所以先放的总是最重要的那个，被挤掉的只是次要信息——
    比按像素顺序画可靠，也不需要引入字体度量依赖。
    """

    def __init__(self, width: int, height: int, cell: int = 8):
        self.cell = max(2, int(cell))
        self.cols = max(1, int(math.ceil(width / self.cell)))
        self.rows = max(1, int(math.ceil(height / self.cell)))
        self._used = bytearray(self.rows * self.cols)
        self.placed = 0
        self.skipped = 0

    def try_place(self, x: float, y: float, w: float, h: float) -> bool:
        c0 = max(0, int(x // self.cell))
        c1 = min(self.cols - 1, int((x + w) // self.cell))
        r0 = max(0, int(y // self.cell))
        r1 = min(self.rows - 1, int((y + h) // self.cell))
        if c1 < c0 or r1 < r0:
            self.skipped += 1
            return False
        for r in range(r0, r1 + 1):
            base = r * self.cols
            for c in range(c0, c1 + 1):
                if self._used[base + c]:
                    self.skipped += 1
                    return False
        for r in range(r0, r1 + 1):
            base = r * self.cols
            for c in range(c0, c1 + 1):
                self._used[base + c] = 1
        self.placed += 1
        return True


def _label_box(text: str, x: float, y: float, draw=None, font=None) -> tuple:
    """文字占位框。能实测就实测：汉字宽度约为等宽 ASCII 的两倍，
    按字符数估算会低估一半，避让网格就会让标签互相压住。"""
    if draw is not None and font is not None:
        try:
            left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
            return (x, y, right - left + 4.0, bottom - top + 3.0)
        except (AttributeError, OSError, ValueError):
            pass
    return (x, y, len(text) * 6.2 + 3.0, 12.0)


def draw_overlay(rgb: np.ndarray, overlay: OverlayData, line_width: int = 2,
                 show_labels: bool = True, avoid_overlap: bool = True,
                 max_labels: Optional[int] = None) -> np.ndarray:
    """在 (H,W,3) uint8 上绘制标注，返回新数组。

    avoid_overlap 为真时冲突的文字会被跳过，标记符号本身始终绘制。
    """
    from PIL import Image, ImageDraw

    from ..core.font import text_font

    img = Image.fromarray(np.ascontiguousarray(rgb))
    draw = ImageDraw.Draw(img)
    font = text_font(11)
    h, w = rgb.shape[0], rgb.shape[1]
    placer = LabelPlacer(w, h) if avoid_overlap else None
    budget = max_labels if max_labels is not None else max(24, min(w, h) // 6)
    drawn = 0

    def tag(x: float, y: float, text: str, color: tuple) -> None:
        nonlocal drawn
        if not text or drawn >= budget:
            return
        text = text[:40]
        bx, by, bw, bh = _label_box(text, x, y, draw, font)
        if placer is not None and not placer.try_place(bx, by, bw, bh):
            return
        draw.text((bx + 2, by + 1), text, fill=color, font=font)
        drawn += 1

    for i, b in enumerate(overlay.boxes):
        col = _color_of(b, i)
        draw.rectangle([b.x0, b.y0, b.x1, b.y1], outline=col, width=line_width)
        if show_labels:
            tag(b.x1 + 2, b.y0, b.label, col)

    for i, p in enumerate(overlay.points):
        col = _color_of(p, i)
        if p.cross:
            r = p.radius
            draw.line([p.x - r, p.y, p.x + r, p.y], fill=col, width=1)
            draw.line([p.x, p.y - r, p.x, p.y + r], fill=col, width=1)
        else:
            draw.ellipse([p.x - p.radius, p.y - p.radius, p.x + p.radius, p.y + p.radius], outline=col)
        if show_labels:
            tag(p.x + p.radius + 2, p.y - 6, p.label, col)

    for i, pl in enumerate(overlay.polylines):
        pts = [(float(a), float(b)) for a, b in pl.pts]
        if len(pts) >= 2:
            draw.line(pts, fill=_color_of(pl, i), width=line_width)
            if show_labels:
                tag(pts[0][0] + 2, pts[0][1] - 6, pl.label, _color_of(pl, i))

    for x, y, text, col in overlay.text:
        draw.text((x, y), text, fill=col or (230, 230, 230), font=font)

    return np.asarray(img)


def render_intervals(width: int, height: int, t0: float, t1: float,
                     intervals: list, lanes: Optional[Sequence[str]] = None) -> np.ndarray:
    """把时间区间画成泳道条带，用于事件轴。"""
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    canvas[:] = (18, 18, 22)
    if not intervals or width <= 0 or height <= 0 or t1 <= t0:
        return canvas

    from PIL import Image, ImageDraw

    img = Image.fromarray(canvas)
    draw = ImageDraw.Draw(img)

    lanes = list(lanes) if lanes else sorted({iv.level for iv in intervals})
    lane_h = max(4, height // max(len(lanes), 1))
    span = t1 - t0

    for iv in intervals:
        lane = lanes.index(iv.level) if iv.level in lanes else 0
        x0 = (iv.t0 - t0) / span * width
        x1 = (min(iv.t1, t1) - t0) / span * width
        x1 = max(x1, x0 + 1)
        y0 = lane * lane_h + 1
        draw.rectangle([x0, y0, x1, min(y0 + lane_h - 2, height - 1)],
                       fill=_color_of(iv, lane), outline=(255, 255, 255))

    return np.asarray(img)
