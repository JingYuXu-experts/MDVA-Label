"""L4 标注层：几何模型、存储、审核状态机。

`prompts`（视觉提示传播）与 `active`（主动学习）故意不在这里 eager import：它们要拉
L5 的 `backends`，而 `backends` 又要用本层的 `Ann`。两边都在包初始化时互引，
`import mdva.backends` 就会撞成循环导入（CLI 的第一个 import 正是它）。要用智能标注
就按模块名导入：`from mdva.annotate.prompts import propagate`。
"""

from . import model, state, store
from .model import SOURCES, Ann, merge
from .store import DATASET_TYPES, Store

__all__ = ["Ann", "DATASET_TYPES", "SOURCES", "Store", "merge", "model", "state", "store"]
