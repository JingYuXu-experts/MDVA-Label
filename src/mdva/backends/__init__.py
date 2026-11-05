"""L5 后端层：算法/模型的可替换注册表。

导入本包即完成三类注册：九个经典算子后端（classic）、一个视觉提示后端（visual）、
三个深度学习占位后端（dl_stub）。`BACKENDS` 与 L1/L2/L3 用的是同一个 Registry 实现，
所以用户插件目录里的 `BACKENDS.add(...)` 或装饰器同样生效——这就是"算法替换"的落点。
"""

from __future__ import annotations

from . import base, bench, classic, dl_stub, probe, recommend, visual
from .base import BACKENDS, Backend, BackendManifest, Prompt, catalog, pick
from .bench import Row, default_table_path, load_table, measure
from .bench import run as bench_run
from .bench import save_table as bench_save
from .probe import Probe, detect
from .recommend import rank, score, top3, usable


def names() -> list:
    return BACKENDS.names()


def available_names(which=None) -> list:
    """可用的后端名字；默认排除深度学习占位条目。"""
    which = which if which is not None else detect()
    out = []
    for entry in BACKENDS:
        ok, _why = entry.create().available(which)
        if ok:
            out.append(entry.name)
    return out


def load_plugin_dir(directory) -> list:
    """热加载用户自带的后端插件（与算子插件同一套机制）。"""
    return BACKENDS.load_plugins(directory)


__all__ = ["BACKENDS", "Backend", "BackendManifest", "Probe", "Prompt", "Row",
           "available_names", "base", "bench", "bench_run", "bench_save", "catalog",
           "classic", "default_table_path", "detect", "dl_stub", "load_plugin_dir",
           "load_table", "measure", "names", "pick", "probe", "rank", "recommend", "score",
           "top3", "usable", "visual"]
