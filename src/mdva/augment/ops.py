"""L4 增强算子：几何链、光度扰动、时序抖动。

三条硬规矩，都是为了让"增强后的数据集还能用"这件事可核验：

1. 先抽参数再执行。每个算子 `sample(rng)` 出一份具体参数（含派生种子），它进 JSON、
   进数据库，重放时按同一份参数重建——同 seed 必须得到同一张图，否则论文里的
   消融表复现不出来。
2. 几何变换只用整数索引操作（rot90 / flip / 裁剪）。掩膜是类别编号不是强度，
   一旦插值就会把两类糊成第三类，所以掩膜走和图像完全相同的索引链；
   光度扰动对掩膜是恒等变换。
3. 时序增强只碰连续通道。离散通道（工况码、开关量）加噪或缩放后会出现标定表里
   没登记过的码值，那是假工况，比不增强更有害。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

from ..core.model import Modality, TimeSeries
from ..core.registry import Registry, make_register_func

AUG_OPS = Registry("增强算子")
register = make_register_func(AUG_OPS)

IMAGE_LIKE = (Modality.IMAGE.value, Modality.CUBE.value, Modality.VIDEO.value)
SERIES = (Modality.SERIES.value,)

#: 派生种子写进谱系，所以噪声既可重放又不至于每张图共用同一条随机序列
MAX_SEED = 2 ** 31 - 1


def _dyn_range(a: np.ndarray) -> float:
    """有限值之间的动态范围。

    科学格式里一个 NaN 就能让 min/max 全变 NaN，那样"按量程归一的噪声"就成了噪声本身。
    """
    f = a[np.isfinite(a)]
    if f.size == 0:
        return 0.0
    return float(f.max() - f.min())


def _col_span(values: np.ndarray, col: int) -> float:
    v = values[:, col]
    v = v[np.isfinite(v)]
    if v.size == 0:
        return 0.0
    return float(v.max() - v.min())


def crop_box(shape: tuple, fraction: float, ox: float = 0.0, oy: float = 0.0) -> tuple:
    """按边长保留比例 + 中心偏移（[-1,1]，0 为正中）算出整数裁剪框。

    `apply()` 和 `chain()` 都调这一个函数，所以界面预览里的裁剪框和落盘裁剪结果是同一个。
    """
    h, w = int(shape[-2]), int(shape[-1])
    frac = min(max(float(fraction), 1.0 / max(h, w)), 1.0)
    nw, nh = max(1, int(round(w * frac))), max(1, int(round(h * frac)))
    dx = int(round((w - nw) * 0.5 * float(ox)))
    dy = int(round((h - nh) * 0.5 * float(oy)))
    x0 = int(np.clip((w - nw) // 2 + dx, 0, w - nw))
    y0 = int(np.clip((h - nh) // 2 + dy, 0, h - nh))
    return (x0, y0, x0 + nw, y0 + nh)


def _plain(obj):
    """numpy 标量不能 json.dumps，谱系要能原样写进 SQLite。"""
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


@dataclass
class Spec:
    """一次具体增强：算子名 + 抽出来的参数。进 JSON、进数据库、可重放。"""

    op: str
    params: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"op": self.op, "params": _plain(self.params)}

    @classmethod
    def from_json(cls, obj: dict) -> "Spec":
        return cls(str(obj["op"]), dict(obj.get("params") or {}))


class Aug:
    """增强算子基类。几何类算子额外提供 `chain()`，把自身翻译成清洗层的整数索引链。"""

    name = "aug"
    modalities = IMAGE_LIKE
    geom = False

    def __init__(self, **params):
        self.params = dict(params)
        for key, spec in AUG_OPS.get(self.name).schema.items():
            self.params.setdefault(key, spec.get("default"))

    def param(self, key):
        return self.params.get(key, AUG_OPS.get(self.name).schema.get(key, {}).get("default"))

    def handles(self, modality: str) -> bool:
        return modality in self.modalities

    def sample(self, rng: np.random.Generator) -> dict:
        """从参数范围里抽一次具体配置；返回的字典必须可 JSON 序列化。"""
        raise NotImplementedError

    def apply(self, arr: np.ndarray, params: dict) -> np.ndarray:
        raise NotImplementedError

    def mask(self, m: np.ndarray, params: dict) -> np.ndarray:
        """掩膜跟着几何链走；光度扰动对掩膜是恒等（编号不是强度）。"""
        return self.apply(m, params) if self.geom else m

    def series(self, ts: TimeSeries, params: dict) -> TimeSeries:
        raise NotImplementedError

    def chain(self, params: dict, shape: tuple) -> list:
        """几何链，交给 `clean.ops.map_geometry` 搬标注——避免两套坐标换算。"""
        return []


# ---------------------------------------------------------------------- 图像类算子

@register("photometric", "光度扰动（亮度 / 对比度 / 高斯噪声）", {
    "brightness": {"type": "number", "default": 0.15, "min": 0.0, "max": 1.0,
                   "description": "亮度偏移幅度，单位是动态范围的比例"},
    "contrast": {"type": "number", "default": 0.15, "min": 0.0, "max": 1.0,
                 "description": "对比度增益的浮动幅度"},
    "noise": {"type": "number", "default": 0.01, "min": 0.0, "max": 0.5,
              "description": "高斯噪声标准差，单位是动态范围的比例"},
    "p": {"type": "number", "default": 1.0, "min": 0.0, "max": 1.0,
          "description": "这一路真正生效的概率"},
})
class Photometric(Aug):
    name = "photometric"

    def sample(self, rng) -> dict:
        b, c = float(self.param("brightness")), float(self.param("contrast"))
        return {"db": float(rng.uniform(-b, b)),
                "gain": float(rng.uniform(1.0 - c, 1.0 + c)),
                "sigma": float(self.param("noise")),
                "seed": int(rng.integers(0, MAX_SEED)),
                "active": bool(rng.random() < float(self.param("p")))}

    def apply(self, arr, params) -> np.ndarray:
        a = np.asarray(arr)
        if not params.get("active", True):
            return a
        f = a.astype(np.float64)
        span = _dyn_range(f)
        if span <= 0:
            return f
        mean = float(np.mean(f[np.isfinite(f)]))
        out = mean + (f - mean) * float(params["gain"]) + float(params["db"]) * span
        sigma = float(params.get("sigma", 0.0)) * span
        if sigma > 0:
            noise = np.random.default_rng(int(params["seed"])).normal(0.0, sigma, f.shape)
            out = out + noise
        return np.where(np.isfinite(f), out, f)


@register("rotate90", "旋转 90° 整数倍（掩膜免插值）", {
    "k_choices": {"type": "array", "default": [0, 1, 2, 3],
                  "description": "候选的逆时针 90° 次数"},
    "p": {"type": "number", "default": 1.0, "min": 0.0, "max": 1.0},
})
class Rotate90(Aug):
    name = "rotate90"
    geom = True

    def sample(self, rng) -> dict:
        choices = list(self.param("k_choices")) or [0]
        on = bool(rng.random() < float(self.param("p")))
        return {"k": int(rng.choice(choices)) if on else 0}

    def apply(self, arr, params) -> np.ndarray:
        return np.rot90(np.asarray(arr), int(params["k"]) % 4)

    def chain(self, params, shape) -> list:
        k = int(params["k"]) % 4
        return [("rotate", {"k": k})] if k else []


@register("flip", "镜像（水平 / 垂直各按概率）", {
    "p_horizontal": {"type": "number", "default": 0.5, "min": 0.0, "max": 1.0},
    "p_vertical": {"type": "number", "default": 0.0, "min": 0.0, "max": 1.0,
                   "description": "星图/医学轴位图默认不翻上下，翻了下位置就反了"},
})
class Flip(Aug):
    name = "flip"
    geom = True

    def sample(self, rng) -> dict:
        return {"horizontal": bool(rng.random() < float(self.param("p_horizontal"))),
                "vertical": bool(rng.random() < float(self.param("p_vertical")))}

    def apply(self, arr, params) -> np.ndarray:
        a = np.asarray(arr)
        if params.get("horizontal"):
            a = a[..., ::-1]                      # 最后一轴是 x（(H,W) 与 (H,W,C) 都成立）
        if params.get("vertical"):
            a = a[::-1]
        return a

    def chain(self, params, shape) -> list:
        out = []
        if params.get("horizontal"):
            out.append(("mirror", {"horizontal": True}))
        if params.get("vertical"):
            out.append(("mirror", {"horizontal": False}))
        return out


@register("crop", "随机裁剪（比例与中心偏移都进谱系）", {
    "fraction": {"type": "number", "default": 0.8, "min": 0.1, "max": 1.0,
                 "description": "期望保留的边长比例"},
    "fraction_jitter": {"type": "number", "default": 0.1, "min": 0.0, "max": 0.4},
    "offset": {"type": "number", "default": 0.5, "min": 0.0, "max": 1.0,
               "description": "裁剪框中心相对正中的最大偏移（按可移动余量的比例）"},
})
class Crop(Aug):
    name = "crop"
    geom = True

    def sample(self, rng) -> dict:
        lim = float(self.param("offset"))
        frac = float(np.clip(rng.normal(float(self.param("fraction")),
                                        float(self.param("fraction_jitter"))), 0.1, 1.0))
        return {"fraction": frac, "ox": float(rng.uniform(-lim, lim)),
                "oy": float(rng.uniform(-lim, lim))}

    def apply(self, arr, params) -> np.ndarray:
        a = np.asarray(arr)
        x0, y0, x1, y1 = crop_box(a.shape, params["fraction"], params["ox"], params["oy"])
        return a[..., y0:y1, x0:x1]

    def chain(self, params, shape) -> list:
        box = crop_box(shape, params["fraction"], params["ox"], params["oy"])
        if tuple(box) == (0, 0, int(shape[-1]), int(shape[-2])):
            return []
        return [("crop", {"box": [int(v) for v in box]})]


# ---------------------------------------------------------------------- 时序类算子

@register("series_jitter", "时序抖动（连续通道加噪 + 增益，离散通道不动）", {
    "sigma": {"type": "number", "default": 0.01, "min": 0.0, "max": 0.5,
              "description": "噪声标准差，按各通道自身量程归一"},
    "gain": {"type": "number", "default": 0.05, "min": 0.0, "max": 0.5,
             "description": "增益浮动幅度，只作用于连续通道"},
    "p": {"type": "number", "default": 1.0, "min": 0.0, "max": 1.0},
})
class SeriesJitter(Aug):
    name = "series_jitter"
    modalities = SERIES

    def sample(self, rng) -> dict:
        return {"seed": int(rng.integers(0, MAX_SEED)),
                "active": bool(rng.random() < float(self.param("p")))}

    def series(self, ts: TimeSeries, params: dict) -> TimeSeries:
        if not params.get("active", True):
            return ts
        values = np.asarray(ts.values, dtype=np.float64)
        rng = np.random.default_rng(int(params["seed"]))
        noise = rng.standard_normal(values.shape)
        gains = rng.uniform(1.0 - float(self.param("gain")),
                            1.0 + float(self.param("gain")), values.shape[1])
        out = values.copy()
        discrete = np.asarray(ts.discrete, dtype=bool)
        for col in range(values.shape[1]):
            if discrete[col]:
                continue                          # 工况码加噪 = 造出没登记过的工况
            span = _col_span(values, col)
            sigma = float(self.param("sigma")) * span if span > 0 else 0.0
            finite = np.isfinite(values[:, col])
            shifted = values[:, col] * float(gains[col])
            out[:, col] = np.where(finite, shifted + noise[:, col] * sigma, values[:, col])
        return TimeSeries(t=np.array(ts.t, dtype=np.float64, copy=True), values=out,
                          channels=list(ts.channels), units=list(ts.units),
                          discrete=list(ts.discrete))


@register("series_offset", "时间窗平移（曲线与时间轴一起挪，区间标注跟着一起挪）", {
    "max_shift": {"type": "number", "default": 30.0, "min": 0.0,
                  "description": "最大平移秒数"},
})
class SeriesOffset(Aug):
    """只挪时间轴、数值一字不变：离散通道的码值仍然合法，绝对秒数表示的区间标注同步平移。"""

    name = "series_offset"
    modalities = SERIES

    def sample(self, rng) -> dict:
        m = float(self.param("max_shift"))
        return {"dt": float(rng.uniform(-m, m))}

    def series(self, ts: TimeSeries, params: dict) -> TimeSeries:
        return TimeSeries(t=np.array(ts.t, dtype=np.float64) + float(params["dt"]),
                          values=np.array(ts.values, dtype=np.float64, copy=True),
                          channels=list(ts.channels), units=list(ts.units),
                          discrete=list(ts.discrete))

    def chain(self, params, shape) -> list:
        dt = float(params["dt"])
        return [("shift_time", {"dt": dt})] if dt else []


# ---------------------------------------------------------------------- 链执行

def build(specs: Sequence) -> list:
    """[{name, params}] / 名字 / 实例 -> 算子实例列表。"""
    out = []
    for spec in specs or []:
        if isinstance(spec, Aug):
            out.append(spec)
            continue
        if isinstance(spec, str):
            spec = {"name": spec}
        name = spec.get("name") or spec.get("op")
        if not name:
            raise ValueError(f"增强算子缺少 name：{spec}")
        if name not in AUG_OPS:
            raise KeyError(f"未知增强算子 {name}，可用 {AUG_OPS.names()}")
        params = dict(spec.get("params") or {})
        for key in spec:
            if key not in ("name", "op", "params"):
                params[key] = spec[key]
        out.append(AUG_OPS.get(name).create(**params))
    return out


def draw(ops: Sequence[Aug], rng: np.random.Generator) -> list:
    """按算子顺序各抽一份参数 -> [Spec]。这一份就是可重放的谱系。"""
    return [Spec(op.name, op.sample(rng)) for op in ops]


def apply_image(arr: np.ndarray, mask: Optional[np.ndarray], specs: Sequence[Spec],
                modality: str = Modality.IMAGE.value) -> tuple:
    """跑一条链，返回 (图, 掩膜, 几何链)。

    几何链逐步带着"该算子作用前的形状"去 `chain()`，所以标注映射
    （`clean.ops.map_geometry`）拿到的框和这里切出来的像素是同一批数。
    """
    a = np.asarray(arr)
    m = None if mask is None else np.asarray(mask)
    shape, geometry = a.shape, []
    for spec in specs:
        op = AUG_OPS.get(spec.op).create(**spec.params)
        if not op.handles(modality):
            continue
        geometry.extend(op.chain(spec.params, shape))
        a = op.apply(a, spec.params)
        if m is not None:
            m = op.mask(m, spec.params)
        shape = a.shape
    return a, m, geometry


def apply_series(ts: TimeSeries, specs: Sequence[Spec]) -> tuple:
    """跑一条时序链，返回 (新序列, 几何链)。

    值和链在同一次遍历里产出，所以"时间轴挪了多少"和"标注该挪多少"必然来自
    同一份参数——分开跑就会出现曲线走了、区间标注留在原地的错。
    """
    out, chain = ts, []
    for spec in specs:
        op = AUG_OPS.get(spec.op).create(**spec.params)
        if not op.handles(Modality.SERIES.value):
            continue
        chain.extend(op.chain(spec.params, out.values.shape))
        out = op.series(out, spec.params)
    return out, chain


__all__ = ["AUG_OPS", "IMAGE_LIKE", "MAX_SEED", "SERIES", "Aug", "Crop", "Flip",
           "Photometric", "Rotate90", "SeriesJitter", "SeriesOffset", "Spec",
           "apply_image", "apply_series", "build", "crop_box", "draw",
           "make_register_func", "register"]
