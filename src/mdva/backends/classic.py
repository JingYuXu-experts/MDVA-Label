"""经典算子后端：把 L3 的九个分析算子包装成"可替换后端"。

不复制任何算法逻辑——每个后端只是 `analysis.get(name).run(item, window)` 的一层薄壳，
把 `AnalyzeResult.overlay` 翻成 `list[Ann]`。好处是后端列表和分析能力永远同步：
新写一个算子注册进 ANALYZERS，这里就自动多出一个可热切换的后端（G3 的"≥5 后端"由此成立）。
"""

from __future__ import annotations

from typing import Optional, Sequence

from .. import analysis
from ..annotate.model import Ann
from ..core.model import DataItem, Slice
from ..core.registry import ANALYZERS
from .base import BACKENDS, Backend, BackendManifest

TITLES = {
    "star_detect": "点源检测（局部峰值 + 质心）",
    "cosmic_reject": "宇宙线剔除（邻域中值偏离）",
    "bg_stats": "背景统计（异常亮区）",
    "image_quality": "图像质量（清晰度/饱和/坏列）",
    "dynamic_threshold": "动态阈值越界（滑动 MAD）",
    "changepoint": "均值突变点检测",
    "rolling_stats": "滑动统计离群",
    "iforest": "孤立森林无监督异常",
    "correlation": "通道相关性（耦合异常定位）",
}

TASK_OF = {
    "star_detect": ("detect_source",),
    "cosmic_reject": ("reject_cosmic",),
    "bg_stats": ("quality_flag",),
    "image_quality": ("quality_flag",),
    "dynamic_threshold": ("series_anomaly", "interval"),
    "changepoint": ("changepoint", "interval"),
    "rolling_stats": ("series_anomaly", "interval"),
    "iforest": ("series_anomaly", "interval"),
    "correlation": ("correlation",),
}

#: flag.score 的量纲各家不同（MAD 倍数、统计量、异常分），按实测范围归一后才能横向比。
#: 没有条目的后端不提供不确定性排序（主动学习会自动退回检测密度）。
SCORE_SCALE = {"dynamic_threshold": 8.0, "changepoint": 6.0, "rolling_stats": 5.0,
               "iforest": 1.0, "correlation": 1.0}

DOMAIN_HINT = {"star_detect": ("aerospace", "medical", "cv_segmentation"),
               "image_quality": ("aerospace", "medical", "cv_segmentation"),
               "dynamic_threshold": ("aerospace", "medical"),
               "changepoint": ("aerospace", "medical"),
               "rolling_stats": ("aerospace", "medical"),
               "iforest": ("aerospace", "medical"),
               "correlation": ("aerospace", "medical"),
               "cosmic_reject": ("aerospace", "medical", "cv_segmentation"),
               "bg_stats": ("aerospace", "medical", "cv_segmentation")}


def overlay_to_anns(overlay, backend: str, source: str = "preannotate",
                    slice_index: Optional[int] = None) -> list:
    """L2 叠加对象 -> L4 标注对象。画布与后端预览共用同一套几何，因此必须一一对应。"""
    out = []
    for kind in ("boxes", "points", "polylines", "intervals"):
        for obj in getattr(overlay, kind):
            out.append(Ann.from_overlay(obj, source=source, backend=backend,
                                        slice=slice_index))
    return out


class AnalyzerBackend(Backend):
    """一个算子 = 一个后端。参数表直接复用算子的 schema，界面不用维护两份。"""

    def __init__(self, analyzer: str = "", **params):
        self.analyzer = analyzer or getattr(self, "analyzer", "")
        entry = ANALYZERS.get(self.analyzer)
        self.entry = entry
        self.manifest = BackendManifest(
            name=self.analyzer,
            title=TITLES.get(self.analyzer, self.analyzer),
            family="classic",
            tasks=TASK_OF.get(self.analyzer, ("anomaly",)),
            modalities=tuple(getattr(entry.factory, "modality", ()) or ("image", "cube", "series")),
            domains=DOMAIN_HINT.get(self.analyzer, ("*",)),
            schema=dict(entry.schema or {}),
            note=entry.description or "",
        )
        super().__init__(**params)

    def available(self, probe=None) -> tuple:
        return (True, "")

    def diagnose(self, item: DataItem, window: Optional[Slice] = None):
        inst = self.entry.create(**self.params)
        if not inst.handles(item):
            raise ValueError(f"{item.modality} 数据不能用 {self.analyzer} 处理"
                             f"（该算子支持 {inst.modality}）")
        return inst.run(item, window or Slice.full())

    def predict(self, item: DataItem, prompts: Sequence = (),
                window: Optional[Slice] = None) -> list:
        result = self.diagnose(item, window)
        index = None
        if item.n_views > 1:
            index = int((window or Slice.full()).index or 0)
        return overlay_to_anns(result.overlay, self.analyzer, slice_index=index)

    def metrics(self, item: DataItem, window: Optional[Slice] = None) -> dict:
        return self.diagnose(item, window).metrics

    def flags(self, item: DataItem, window: Optional[Slice] = None) -> list:
        return self.diagnose(item, window).flags

    def uncertainty(self, item: DataItem, window: Optional[Slice] = None) -> Optional[float]:
        """送给主动学习的"这一路有多可疑"。时序用越界幅度，图像用检测密度。"""
        result = self.diagnose(item, window)
        scale = SCORE_SCALE.get(self.analyzer)
        scores = [abs(f.score) for f in result.flags if f.score]
        if scale and scores:
            return float(min(1.0, max(scores) / scale))
        if not scores:
            return float(min(1.0, result.overlay.count() / 40.0))
        return None


def build(analyzer: str) -> AnalyzerBackend:
    return AnalyzerBackend(analyzer=analyzer)


def _register_all() -> None:
    for entry in ANALYZERS:
        name = entry.name
        BACKENDS.add(
            name,
            lambda _n=name, **kw: AnalyzerBackend(analyzer=_n, **kw),
            TITLES.get(name, name),
            entry.schema,
        )


_register_all()


def names() -> list:
    """经典后端的名字就是算子名，两边保持一份清单。"""
    return analysis.names()


__all__ = ["AnalyzerBackend", "build", "names", "overlay_to_anns", "BACKENDS"]
