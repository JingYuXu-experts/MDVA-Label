"""L5 后端层：算法/模型的统一可替换接口。

计划书原本要接 T-Rex2 / GroundingDINO / YOLO-World，用户明确要求**完全不碰大模型**，
所以这里的后端全部是零显存、零下载的经典算法。接口形状仍然按"可替换后端"设计：
`available()` 返回 (能不能用, 原因) 而不是抛异常，`manifest()` 决定界面能不能选它，
`predict()` 是预标注/视觉提示传播/内容过滤三者共用的唯一出口。
这样将来真要接深度模型，只是往同一张注册表里多塞一个条目，上层一行不改。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

import numpy as np

from ..annotate.model import Ann
from ..core.model import DataItem, Slice
from ..core.registry import Registry, make_register_func

BACKENDS = Registry("后端")
register = make_register_func(BACKENDS)

FAMILIES = ("classic", "visual_prompt", "deep_stub")
TASKS = ("detect_source", "reject_cosmic", "quality_flag", "series_anomaly", "changepoint",
         "correlation", "detect_similar", "propagate", "content_filter", "interval")


@dataclass
class Prompt:
    """一次视觉提示。kind/coords 与标注几何同构，因此提示本身可以直接落库留痕。

    `source_uri` + `template` 让提示可以跨数据项传播：传播时把源图上的那块像素
    带在提示里，目标项不用再见到源图也能匹配。
    """

    kind: str = "box"
    coords: list = field(default_factory=list)
    label: str = ""
    role: str = "positive"
    source_uri: str = ""
    template: Optional[np.ndarray] = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        self.kind = str(self.kind)
        if self.kind not in ("box", "point", "poly", "interval", "text"):
            raise ValueError(f"未知提示类型 {self.kind!r}")
        if self.kind == "text":
            raise ValueError("本平台不接入语言模型，text 提示不可用")
        if self.role not in ("positive", "negative"):
            raise ValueError(f"未知提示角色 {self.role}")
        self.coords = [float(c) for c in self.coords]
        need = {"box": 4, "point": 2, "interval": 2}.get(self.kind)
        if need is not None and len(self.coords) != need:
            raise ValueError(f"{self.kind} 提示需要 {need} 个坐标，收到 {len(self.coords)}")
        if self.kind == "poly" and (len(self.coords) < 6 or len(self.coords) % 2):
            raise ValueError("poly 提示需要 >=3 个顶点的扁平坐标")

    @property
    def box(self) -> tuple:
        return tuple(self.coords)

    def to_ann(self, source: str = "visual_prompt", backend: str = "") -> Ann:
        return Ann(self.kind, list(self.coords), self.label, source=source,
                   confidence=1.0, backend=backend)

    def to_json(self) -> dict:
        return {"kind": self.kind, "coords": list(self.coords), "label": self.label,
                "role": self.role, "source_uri": self.source_uri}

    @classmethod
    def from_json(cls, payload: dict) -> "Prompt":
        return cls(payload.get("kind", "box"), payload.get("coords") or [],
                   payload.get("label", ""), payload.get("role", "positive"),
                   payload.get("source_uri", ""))


@dataclass
class BackendManifest:
    name: str
    title: str
    family: str
    tasks: tuple = ()
    modalities: tuple = ()
    domains: tuple = ("*",)        # 写成 ("*") 是字符串 "*"，会被当成"只服务名为 * 的学科"
    schema: dict = field(default_factory=dict)
    supports_prompt: bool = False
    prompt_kinds: tuple = ()
    needs_gpu: bool = False
    needs_weights: bool = False
    license: str = "本项目自研（GPL-3.0）"
    note: str = ""

    def covers(self, modality: str, task: str = "") -> bool:
        """留空的 modality（和 task）表示"不限"，不是要求后端声明一个叫 "*" 的模态。

        `catalog` 的"只按任务筛选"就靠这个语义：过去把 "" 折成 "*" 传进来，等于
        要求模态表里真有 "*"，结果任何按任务的查询都返回空列表。
        """
        if modality and modality != "*" and not (modality in self.modalities
                                                or "*" in self.modalities):
            return False
        return not task or task in self.tasks or "*" in self.tasks

    def to_json(self) -> dict:
        return {"name": self.name, "title": self.title, "family": self.family,
                "tasks": list(self.tasks), "modalities": list(self.modalities),
                "domains": list(self.domains), "supports_prompt": self.supports_prompt,
                "prompt_kinds": list(self.prompt_kinds), "needs_gpu": self.needs_gpu,
                "needs_weights": self.needs_weights, "license": self.license,
                "note": self.note, "params": self.schema}


class Backend(ABC):
    """一个可替换的算法后端。"""

    manifest: BackendManifest

    def __init__(self, **params):
        self.params = dict(params)
        known = set(self.manifest.schema or {})
        if known:
            unknown = [k for k in self.params if k not in known]
            if unknown:
                # 参数名写错会被算子静默忽略，界面改了却看不见区别——这里必须报错
                raise TypeError(f"{self.manifest.name} 不认识参数 {unknown}，可用 {sorted(known)}")
        for key, spec in (self.manifest.schema or {}).items():
            self.params.setdefault(key, spec.get("default"))

    # ------------------------------------------------------------------ 能力
    def available(self, probe=None) -> tuple:
        """经典后端恒为可用；深度学习占位后端返回 (False, 原因)。"""
        return (True, "")

    def handles(self, item: DataItem) -> bool:
        return self.manifest.covers(item.modality)

    def useable_on(self, items: Iterable[DataItem], task: str = "") -> tuple:
        """给界面的一句话：这个后端在当前勾选里能处理几路。"""
        hit = [i for i in items if self.manifest.covers(i.modality, task)]
        return (bool(hit), len(hit))

    @abstractmethod
    def predict(self, item: DataItem, prompts: Sequence[Prompt] = (),
                window: Optional[Slice] = None) -> list:
        """返回标注列表。预标注、同图找相似、跨图传播三条路都走这里。"""

    # ------------------------------------------------------------------ 评测
    def evaluate(self, items: Iterable[DataItem], truth: Optional[dict] = None,
                 task: str = "", limit: Optional[int] = None) -> dict:
        from .bench import measure

        return measure(self, list(items)[: int(limit)] if limit else list(items),
                       truth or {}, task=task or (self.manifest.tasks or ("",))[0])

    # ------------------------------------------------------------------ 辅助
    def run_timed(self, item: DataItem, prompts=(), window=None) -> tuple:
        t0 = time.perf_counter()
        anns = self.predict(item, prompts, window)
        return anns, (time.perf_counter() - t0) * 1000.0

    def describe(self) -> str:
        m = self.manifest
        return (f"{m.title}｜{'/'.join(m.tasks) or '通用'}｜模态 {'/'.join(m.modalities)}"
                f"｜{'需显卡' if m.needs_gpu else '纯 CPU'}")

    def __repr__(self):
        return f"Backend({self.manifest.name!r})"


def pick(name: str, **params) -> Backend:
    """按名字实例化后端；参数非法时给出可用参数表而不是裸 TypeError。"""
    entry = BACKENDS.get(name)
    try:
        return entry.create(**params)
    except TypeError as exc:
        allowed = sorted(entry.schema or {})
        raise TypeError(f"{name} 不接受该参数：{exc}；可用参数 {allowed}") from None


def catalog(probe=None, domain: str = "", task: str = "", modality: str = "") -> list:
    """列出后端及其可用性，供前端参数面板与"算法替换"下拉框直接渲染。"""
    out = []
    for entry in BACKENDS:
        inst = entry.create()
        ok, why = inst.available(probe)
        m = inst.manifest
        if domain and m.domains != ("*",) and domain not in m.domains:
            continue
        if not m.covers(modality, task):
            continue
        out.append({**m.to_json(), "available": ok, "reason": why})
    return out


__all__ = ["BACKENDS", "Backend", "BackendManifest", "FAMILIES", "Prompt", "TASKS",
           "catalog", "pick", "register"]
