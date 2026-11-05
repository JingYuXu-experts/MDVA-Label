"""L4 增强层：算子、学科策略、seed 谱系与重放。

与清洗层的分工：清洗的 crop/rotate/mirror 是"把数据修对"（一次一条链，结果替换条目），
增强的同名算子是"把样本变多"（一份配方 × factor 个副本，原件与父版本都保留）。
两层的几何变换共用 `clean.ops.map_geometry`，所以不会出现两套坐标换算。
"""

from . import lineage, ops, policy
from .lineage import Job, Recipe, plan, read_lineage, replay, run, write_series_csv
from .ops import (AUG_OPS, Aug, Crop, Flip, Photometric, Rotate90, SeriesJitter,
                  SeriesOffset, Spec, apply_image, apply_series, build, crop_box, draw,
                  register)
from .policy import PRESETS, check, get, presets, suggest

__all__ = ["AUG_OPS", "Aug", "Crop", "Flip", "Job", "Photometric", "PRESETS", "Recipe",
           "Rotate90", "SeriesJitter", "SeriesOffset", "Spec", "apply_image", "apply_series",
           "build", "check", "crop_box", "draw", "get", "lineage", "ops", "plan", "policy",
           "presets", "read_lineage", "register", "replay", "run", "suggest",
           "write_series_csv"]
