"""统一多模态数据模型。

四类模态（图像 / 数据立方体 / 多通道时序 / 事件）共用同一组字段，
其中 t_ref 与 Slice 的时间语义是实现跨模态联动的唯一前提：
任何两个数据项只要能把 t_ref 对齐到 UTC 秒，就能被同一条时间轴驱动。
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np

INF = float("inf")


class Modality(str, enum.Enum):
    IMAGE = "image"
    CUBE = "cube"
    SERIES = "series"
    EVENT = "event"
    VIDEO = "video"


@dataclass(frozen=True)
class Slice:
    """一次查看请求的范围。

    图像类模态用 index 定位切片，时序与事件类用 [t0, t1] 定位窗口。
    两种语义共存是为了让网格布局层不必关心模态差异。
    """

    index: int = 0
    t0: float = -INF
    t1: float = INF

    @classmethod
    def at_index(cls, index: int) -> "Slice":
        return cls(index=int(index))

    @classmethod
    def at_time(cls, t0: float, t1: float) -> "Slice":
        return cls(t0=float(t0), t1=float(t1))

    @classmethod
    def full(cls) -> "Slice":
        return cls()

    def contains(self, t: float) -> bool:
        return self.t0 <= t < self.t1

    def nearest_index(self, t: np.ndarray) -> int:
        """在升序时间数组中取窗口起点最近的样本，空窗口返回 0。"""
        if t.size == 0 or not math.isfinite(self.t0):
            return 0
        return int(np.searchsorted(t, self.t0, side="left").clip(0, t.size - 1))


@dataclass
class TimeSeries:
    """多通道等间隔或不规则采样时序。t 必须升序。

    discrete 按通道标记离散量（状态机、开关），渲染与重采样据此
    选择阶梯线与最近取值，避免对状态码做插值或求均值。
    """

    t: np.ndarray
    values: np.ndarray
    channels: list
    units: list = field(default_factory=list)
    discrete: list = field(default_factory=list)

    def __post_init__(self):
        self.t = np.asarray(self.t, dtype=np.float64)
        self.values = np.asarray(self.values, dtype=np.float64)
        if self.values.ndim == 1:
            self.values = self.values[:, None]
        if self.values.shape[0] != self.t.shape[0]:
            raise ValueError(
                f"时间轴长度 {self.t.shape[0]} 与数据行数 {self.values.shape[0]} 不一致"
            )
        if len(self.channels) != self.values.shape[1]:
            self.channels = [f"ch{i}" for i in range(self.values.shape[1])]
        if len(self.units) != self.values.shape[1]:
            self.units = [""] * self.values.shape[1]
        if len(self.discrete) != self.values.shape[1]:
            self.discrete = [False] * self.values.shape[1]
        else:
            self.discrete = [bool(d) for d in self.discrete]

    def is_discrete(self, channel: int) -> bool:
        return bool(self.discrete[channel]) if 0 <= channel < len(self.discrete) else False

    @property
    def n_channels(self) -> int:
        return int(self.values.shape[1])

    @property
    def start(self) -> float:
        return float(self.t[0]) if self.t.size else -INF

    @property
    def end(self) -> float:
        return float(self.t[-1]) if self.t.size else INF

    def window(self, sl: Slice) -> "TimeSeries":
        i0, i1 = self._window_bounds(sl)
        return TimeSeries(
            t=self.t[i0:i1],
            values=self.values[i0:i1],
            channels=list(self.channels),
            units=list(self.units),
            discrete=list(self.discrete),
        )

    def _window_bounds(self, sl: Slice) -> tuple:
        """窗口按左闭右开 [t0, t1)，与 Slice.contains 保持一致。"""
        if self.t.size == 0:
            return 0, 0
        if not math.isfinite(sl.t0) and not math.isfinite(sl.t1):
            return 0, self.t.size
        i0 = int(np.searchsorted(self.t, sl.t0, side="left"))
        i1 = int(np.searchsorted(self.t, sl.t1, side="left"))
        if i1 <= i0:
            i0 = min(i0, self.t.size - 1)
            i1 = i0 + 1
        return i0, i1

    def channel_index(self, name: str) -> Optional[int]:
        try:
            return int(self.channels.index(name))
        except ValueError:
            return None


@dataclass
class EventRow:
    t: float
    label: str
    level: int = 0
    source: str = ""
    t_end: float = INF
    extra: dict = field(default_factory=dict)

    @property
    def is_interval(self) -> bool:
        return math.isfinite(self.t_end) and self.t_end > self.t


@dataclass
class EventTable:
    rows: list

    def __post_init__(self):
        self.rows = sorted(self.rows, key=lambda r: r.t)

    @property
    def start(self) -> float:
        return self.rows[0].t if self.rows else -INF

    @property
    def end(self) -> float:
        return self.rows[-1].t if self.rows else INF

    def select(self, sl: Slice) -> "EventTable":
        return EventTable([r for r in self.rows if sl.contains(r.t) or sl.contains(r.t_end)])

    def levels(self) -> list:
        return sorted({r.level for r in self.rows})

    def by_level(self, level: int) -> "EventTable":
        return EventTable([r for r in self.rows if r.level == level])


@dataclass
class DataItem:
    """一个可展示、可分析的数据项。加载是惰性的，由加载器注入 opener。"""

    id: str
    # 一律存 Modality.X.value：比较也统一用 .value，别处不允许再取 .value
    modality: str
    uri: str
    title: str = ""
    t_ref: Optional[float] = None
    t_end: Optional[float] = None
    meta: dict = field(default_factory=dict)
    shape: tuple = ()
    dtype: str = ""
    n_views: int = 1

    _opener: Callable[[], Any] = field(repr=False, default=None)
    _cache: Any = field(repr=False, default=None)

    @property
    def has_data(self) -> bool:
        return self._cache is not None

    def load(self) -> Any:
        """返回 ndarray(IMAGE/CUBE) / TimeSeries(SERIES) / EventTable(EVENT)。"""
        if self._cache is None:
            if self._opener is None:
                raise ValueError(f"{self.id} 未绑定加载函数")
            self._cache = self._opener()
        return self._cache

    def release(self) -> None:
        self._cache = None

    @property
    def time_span(self) -> tuple:
        if self.t_ref is None:
            return (-INF, INF)
        return (self.t_ref, self.t_end if self.t_end is not None else self.t_ref)

    def overlaps(self, sl: Slice) -> bool:
        a, b = self.time_span
        return not (b < sl.t0 or a > sl.t1)

    def __repr__(self) -> str:
        return f"DataItem({self.id!r}, {self.modality}, views={self.n_views})"
