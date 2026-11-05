"""标注几何模型。

几何语义与 L2 叠加层（`render/overlay.py`）严格一一对应：后端预览图画出来的框，
和网页画布上画的框，必须是同一个东西、同一个颜色规则。否则"预标注质量"没法目视核对。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

GEOMS = ("box", "poly", "point", "interval", "level")

SOURCES = ("manual", "visual_prompt", "preannotate", "active_learning", "import")

#: 与 render.overlay.DEFAULT_COLORS 同序，保证前后端着色一致
PALETTE = ((255, 80, 80), (80, 200, 255), (255, 200, 60), (120, 255, 120), (220, 120, 255))


def _num(v) -> float:
    """SQLite 里存的是文本，NaN/Inf 一律当无效值处理，不静默变成 0。"""
    f = float(v)
    if not math.isfinite(f):
        raise ValueError(f"坐标必须是有限实数，收到 {v!r}")
    return f


@dataclass
class Ann:
    """一条标注。

    `geom` 决定 `coords` 的解释：
      box      [x0, y0, x1, y1]      像素，左上/右下
      poly     [x0, y0, x1, y1, ...] 像素，扁平顶点
      point    [x, y]                像素
      interval [t0, t1]              UTC 秒，时序/事件的区间刷选
      level    [t0, t1] + level      事件区间，同时给出等级
    """

    geom: str
    coords: list = field(default_factory=list)
    label: str = ""
    source: str = "manual"
    confidence: float = 1.0
    backend: str = ""
    slice: Optional[int] = None
    level: int = 0
    color: Optional[tuple] = None
    note: str = ""

    def __post_init__(self):
        self.geom = str(self.geom)
        if self.geom not in GEOMS:
            raise ValueError(f"未知几何 {self.geom!r}，可用 {GEOMS}")
        if self.source not in SOURCES:
            raise ValueError(f"未知来源 {self.source!r}，可用 {SOURCES}")
        self.coords = [_num(c) for c in self.coords]
        self._check_coords()

    def _check_coords(self):
        need = {"box": 4, "point": 2, "interval": 2, "level": 2}
        if self.geom in need and len(self.coords) != need[self.geom]:
            raise ValueError(f"{self.geom} 需要 {need[self.geom]} 个坐标，收到 {len(self.coords)}")
        if self.geom == "poly" and (len(self.coords) < 6 or len(self.coords) % 2):
            raise ValueError(f"poly 需要 >=3 个顶点的扁平坐标，收到 {len(self.coords)} 个数")
        if self.geom in ("interval", "level") and self.coords[1] < self.coords[0]:
            raise ValueError(f"区间终点早于起点：{self.coords}")
        if self.geom == "box" and (self.coords[2] < self.coords[0] or self.coords[3] < self.coords[1]):
            raise ValueError(f"矩形右下必须大于左上：{self.coords}")

    # ------------------------------------------------------------------ 度量

    def bbox(self) -> Optional[tuple]:
        if self.geom == "box":
            return tuple(self.coords)
        if self.geom == "point":
            x, y = self.coords
            return (x, y, x, y)
        if self.geom == "poly":
            xs, ys = self.coords[0::2], self.coords[1::2]
            return (min(xs), min(ys), max(xs), max(ys))
        return None

    def area(self) -> float:
        bb = self.bbox()
        if bb is None:
            return max(0.0, (self.coords[1] - self.coords[0]) if len(self.coords) == 2 else 0.0)
        return max(0.0, bb[2] - bb[0]) * max(0.0, bb[3] - bb[1])

    def duration(self) -> float:
        if self.geom in ("interval", "level"):
            return max(0.0, self.coords[1] - self.coords[0])
        return 0.0

    def iou(self, other: "Ann") -> float:
        a, b = self.bbox(), other.bbox()
        if a is None or b is None:
            return 0.0
        ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
        ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
        inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
        union = self.area() + other.area() - inter
        return inter / union if union > 0 else 0.0

    def time_iou(self, other: "Ann") -> float:
        a, b = self.coords, other.coords
        inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
        union = self.duration() + other.duration() - inter
        return inter / union if union > 0 else 0.0

    def overlaps_time(self, t0: float, t1: float) -> bool:
        if self.geom not in ("interval", "level"):
            return False
        return not (self.coords[1] < t0 or self.coords[0] > t1)

    def rgb(self, fallback: int = 0) -> tuple:
        if self.color:
            return tuple(self.color)
        return PALETTE[fallback % len(PALETTE)]

    # ------------------------------------------------------------------ 变换

    def scaled(self, fx: float, fy: float) -> "Ann":
        """像素坐标缩放；时间类几何不受影响。导出归一化与增强共用这一条路径。"""
        if self.geom in ("interval", "level"):
            return Ann(self.geom, list(self.coords), self.label, self.source,
                       self.confidence, self.backend, self.slice, self.level, None, self.note)
        out = []
        for i, c in enumerate(self.coords):
            out.append(c * (fx if i % 2 == 0 else fy))
        return Ann(self.geom, out, self.label, self.source, self.confidence, self.backend,
                   self.slice, self.level, self.color, self.note)

    def flipped(self, horizontal: bool = True, width: float = 1.0, height: float = 1.0) -> "Ann":
        if self.geom in ("interval", "level"):
            raise ValueError("时间区间没有水平/垂直翻转的语义")
        axis = 0 if horizontal else 1
        span = width if horizontal else height
        out = list(self.coords)
        for i in range(axis, len(out), 2):
            out[i] = span - out[i]
        if self.geom == "box":
            x0, y0, x1, y1 = out
            out = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
        return Ann(self.geom, out, self.label, self.source, self.confidence, self.backend,
                   self.slice, self.level, self.color, self.note)

    # ------------------------------------------------------------------ 互转

    def to_overlay(self):
        """转成 L2 叠加层对象，交给 `render.overlay.draw_overlay` 画进位图。"""
        from ..render import overlay as OV

        color = self.rgb()
        if self.geom == "box":
            x0, y0, x1, y1 = self.coords
            return OV.Box(x0, y0, x1, y1, label=self.label, color=color, score=self.confidence)
        if self.geom == "point":
            return OV.Point(self.coords[0], self.coords[1], label=self.label, color=color)
        if self.geom == "poly":
            pts = list(zip(self.coords[0::2], self.coords[1::2]))
            return OV.Polyline(pts, label=self.label, color=color)
        return OV.VInterval(t0=self.coords[0], t1=self.coords[1],
                           label=self.label or f"等级 {self.level}",
                           color=color, level=self.level)

    @classmethod
    def from_overlay(cls, obj, source: str = "preannotate", backend: str = "",
                     slice: Optional[int] = None) -> "Ann":
        from ..render import overlay as OV

        if isinstance(obj, OV.Box):
            return cls("box", [obj.x0, obj.y0, obj.x1, obj.y1], obj.label, source,
                       float(obj.score), backend, slice)
        if isinstance(obj, OV.Point):
            return cls("point", [obj.x, obj.y], obj.label, source, 1.0, backend, slice)
        if isinstance(obj, OV.Polyline):
            flat = [v for p in obj.pts for v in p]
            return cls("poly", flat, obj.label, source, 1.0, backend, slice)
        if isinstance(obj, OV.VInterval):
            return cls("level" if obj.level else "interval", [obj.t0, obj.t1], obj.label,
                       source, 1.0, backend, slice, int(obj.level))
        raise TypeError(f"无法转换的叠加对象 {type(obj).__name__}")

    # ------------------------------------------------------------------ 序列化

    def to_json(self) -> dict:
        return {
            "geom": self.geom, "coords": list(self.coords), "label": self.label,
            "source": self.source, "confidence": self.confidence, "backend": self.backend,
            "slice": self.slice, "level": self.level,
            "color": list(self.color) if self.color else None, "note": self.note,
        }

    @classmethod
    def from_json(cls, payload: dict) -> "Ann":
        color = payload.get("color")
        return cls(
            payload["geom"], list(payload.get("coords") or []),
            payload.get("label", ""), payload.get("source", "manual"),
            float(payload.get("confidence", 1.0)), payload.get("backend", ""),
            payload.get("slice"), int(payload.get("level", 0) or 0),
            tuple(color) if color else None, payload.get("note", ""),
        )


def merge(anns: list[Ann], iou_thr: float = 0.9) -> list[Ann]:
    """去掉几乎重合的框：预标注反复传播最容易在这里堆重复。"""
    out: list[Ann] = []
    for a in anns:
        if not any(a.geom == b.geom and a.label == b.label and a.iou(b) >= iou_thr for b in out):
            out.append(a)
    return out


__all__ = ["Ann", "GEOMS", "SOURCES", "PALETTE", "merge"]
