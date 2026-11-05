"""L4 清洗层：判定算子、任务编排、预览与一键回退。

`preview()` 与 `apply()` 共用 `task.evaluate()`，所以界面上"将剔除 N 条"和执行结果
必然是同一批；所有算子都不碰原始文件，被剔除的条目只是不进入新版本。
"""

from . import ops, task
from .ops import (CLEAN_OPS, Blur, ContentFilter, Crop, Decision, Dedup, Mirror,
                  Rotate, SeriesClean, digest, laplacian_variance, map_geometry, register,
                  write_manifest)
from .task import (Result, apply, build, chain_of, dropped_of, evaluate, inherit, load_items,
                   preview, producing_task, rollback, write_transforms)

__all__ = ["CLEAN_OPS", "Blur", "ContentFilter", "Crop", "Decision", "Dedup", "Mirror",
           "Result", "Rotate", "SeriesClean", "apply", "build", "chain_of",
           "digest", "dropped_of", "evaluate", "inherit", "laplacian_variance", "load_items",
           "map_geometry", "ops", "preview", "producing_task", "register", "rollback", "task",
           "write_manifest", "write_transforms"]
