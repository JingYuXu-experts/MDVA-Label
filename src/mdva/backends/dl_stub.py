"""深度学习后端占位：接口留好，实现不接。

用户明确要求完全不碰大模型（不引 torch、不下权重），所以这几个后端恒返回
`available() -> (False, 原因)`，界面上灰显并写明原因。它们的价值是**证明可替换**：
`predict()` 的签名、Prompt 结构、返回的 Ann 都与经典后端完全一致，
将来接 T-Rex2/GDINO 只需要在这里换一个实现，上层（画布、预标注、传播、审核）不改一行。
"""

from __future__ import annotations

from typing import Sequence

from ..core.model import DataItem, Slice
from .base import BACKENDS, Backend, BackendManifest

REASON = "本设计不接入深度学习后端（无 torch、无权重、完全离线）；此处仅为可替换接口占位"

_STUBS = {
    "trex2": ("T-Rex2（视觉提示开集检测）", ("detect_similar", "propagate", "content_filter"),
              ("box", "point", "poly", "text"), 8.0),
    "grounding_dino": ("GroundingDINO（文本提示开集检测）", ("content_filter", "detect_source"),
                       ("text", "box"), 6.0),
    "yolo_world": ("YOLO-World（开放词汇检测）", ("detect_source", "content_filter"),
                   ("text",), 4.0),
}


class DeepStubBackend(Backend):
    """深度学习后端的统一占位实现。"""

    def __init__(self, model: str = "trex2", **params):
        title, tasks, kinds, vram = _STUBS.get(model, _STUBS["trex2"])
        self.manifest = BackendManifest(
            name=f"deep:{model}", title=title, family="deep_stub", tasks=tasks,
            modalities=("image", "cube", "video"), domains=("*",), supports_prompt=True,
            prompt_kinds=kinds, needs_gpu=True, needs_weights=True,
            license="第三方模型权重（本仓库不含）",
            note=f"{REASON}；参考显存需求约 {vram} GB",
        )
        super().__init__(**params)

    @property
    def vram_gb(self) -> float:
        return _STUBS.get(self.manifest.name.split(":", 1)[-1], (0, 0, 0, 0))[3]

    def available(self, probe=None) -> tuple:
        return (False, REASON)

    def predict(self, item: DataItem, prompts: Sequence = (),
                window: "Slice | None" = None) -> list:
        raise RuntimeError(REASON)


def build(model: str) -> DeepStubBackend:
    return DeepStubBackend(model=model)


def _register_all() -> None:
    """三个占位条目分别绑定各自的模型名。

    不能用 `@register` 直接叠在类上：注册表存的是同一个类工厂，
    三个条目会全部退化成默认参数的那个模型。
    """
    for model, (title, _tasks, _kinds, _vram) in _STUBS.items():
        BACKENDS.add(f"deep:{model}",
                     lambda _m=model, **kw: DeepStubBackend(model=_m, **kw),
                     f"{title} 占位（未接入，界面灰显）", {})


_register_all()


def stub_names() -> list:
    return sorted(_STUBS)


__all__ = ["DeepStubBackend", "REASON", "build", "stub_names"]
