"""L3 分析层：统一算子接口。

算子只产出几何标注与量化指标，不直接改像素，因此同一结果可同时用于
界面高亮、报告导出和数据集标注导出，也便于对多个算法做同图对比评测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from ..core.model import DataItem, Slice
from ..core.registry import ANALYZERS, make_register_func
from ..render.overlay import OverlayData

register = make_register_func(ANALYZERS)


def to_float(arr) -> np.ndarray:
    """统一转成 float64 再做算术。

    加载器保留原生位深是有意的（uint16 立方体比 float64 省 4 倍内存），
    但 uint16 相减会把负残差回绕成大正数，静默产生错误结果而不报错。
    所有算子入口都必须过这一道。
    """
    a = np.asarray(arr)
    if a.dtype.kind in ("f",):
        return a.astype(np.float64, copy=False)
    return a.astype(np.float64)


# BT.601 亮度权重，与 PIL 的 "L" 模式一致：同一张图在界面里看到的亮度
# 和算子算出来的亮度必须对得上，用三通道平均会偏色（蓝通道权重过高）
_LUMA = (0.299, 0.587, 0.114)


def to_gray(arr) -> np.ndarray:
    """彩色数组归约为二维亮度图。

    几何类算子（卷积、FFT、质心）只能作用在二维上，直接喂 (H,W,3) 会在
    `h, w = arr.shape` 处炸掉；而 rfft2 对三维数组会变换最后两轴，
    算出来的频域半径完全不是图像的频域半径——那种错不会报错，只会给错数。
    """
    a = to_float(arr)
    if a.ndim == 3:
        if a.shape[-1] == 4:
            a = a[..., :3]
        weights = np.asarray(_LUMA[: a.shape[-1]], dtype=np.float64)
        a = a @ weights / weights.sum()
    return a


@dataclass
class Flag:
    """一条可复核的异常结论。"""
    t: float
    label: str
    score: float = 0.0
    channel: str = ""
    level: int = 2
    box: Optional[tuple] = None


@dataclass
class AnalyzeResult:
    overlay: OverlayData = field(default_factory=OverlayData)
    metrics: dict = field(default_factory=dict)
    flags: list = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)
    analyzer: str = ""

    def summary(self) -> str:
        bits = [f"{k}={_fmt(v)}" for k, v in list(self.metrics.items())[:6]]
        bits.append(f"标注 {self.overlay.count()}")
        bits.append(f"事件 {len(self.flags)}")
        return f"[{self.analyzer}] " + "  ".join(bits)


def _fmt(v) -> str:
    if isinstance(v, (float, np.floating)):
        return f"{v:.4g}"
    return str(v)


class Analyzer:
    """算子基类。子类实现 run()，通过 register 装饰器注册。"""

    name = "analyzer"
    modality = ("image", "cube", "series", "event")
    description = ""
    schema: dict = {}

    def __init__(self, **params):
        self.params = params
        for key, spec in self.schema.items():
            self.params.setdefault(key, spec.get("default"))

    def run(self, item: DataItem, window: Optional[Slice] = None) -> AnalyzeResult:
        raise NotImplementedError

    def handles(self, item: DataItem) -> bool:
        return item.modality in self.modality


def get(name: str, **params) -> Analyzer:
    return ANALYZERS.get(name).create(**params)


def names() -> list:
    return ANALYZERS.names()


def run_all(item: DataItem, analyzers, window: Optional[Slice] = None) -> AnalyzeResult:
    """同一数据项串跑多个算子，结果叠加。这是算法对比实验的执行入口。"""
    merged = AnalyzeResult(analyzer=",".join(analyzers))
    for name in analyzers:
        inst = get(name)
        if not inst.handles(item):
            continue
        res = inst.run(item, window)
        res.analyzer = name
        merged.overlay.extend(res.overlay)
        merged.flags.extend(res.flags)
        for k, v in res.metrics.items():
            merged.metrics[f"{name}.{k}"] = v
        for k, v in res.artifacts.items():
            merged.artifacts[f"{name}.{k}"] = v
    return merged
