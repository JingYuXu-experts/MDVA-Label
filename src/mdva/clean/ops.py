"""L? 清洗算子：去相似、去模糊、裁剪/旋转/镜像、内容过滤、时序清洗。

每条算子只有一个判定入口 `evaluate()`。`preview()`（阈值滑杆边上的"会剔除 N 条"）
和 `apply()`（真的动手）都调用它——分两次实现就可能出现"预览说 12 条、实跑删 9 条"，
那种不一致比没有预览更糟。

所有算子都不删原始文件：被剔除的条目只是不进入新版本，
落盘的变换写到新目录并留清单，一键回退只删自己写出去的东西。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np

from ..core.model import Modality, Slice
from ..core.registry import Registry, make_register_func
from ..render.image import pick_slice

CLEAN_OPS = Registry("清洗算子")
register = make_register_func(CLEAN_OPS)

IMAGE_LIKE = (Modality.IMAGE.value, Modality.CUBE.value, Modality.VIDEO.value)
TRANSFORMS = ("crop", "rotate", "mirror")


@dataclass
class Decision:
    """一条判定：要不要动它、动了会写成什么。"""

    uri: str
    op: str
    keep: bool = True
    score: float = 0.0
    reason: str = ""
    transform: Optional[str] = None
    params: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"uri": self.uri, "op": self.op, "keep": self.keep,
                "score": round(self.score, 4), "reason": self.reason,
                "transform": self.transform, "params": self.params}


def _gray(item, index: int = 0) -> np.ndarray:
    from ..analysis.base import to_gray

    arr = np.asarray(pick_slice(item, Slice.at_index(index)), dtype=np.float64)
    return to_gray(arr) if arr.ndim == 3 else arr


def _layout_sig(gray: np.ndarray, blocks: int = 8) -> np.ndarray:
    """分块均值向量：抓"东西摆在哪儿"，不是"亮度分布怎样"。

    噪声主导的科学图里，灰度直方图（连梯度直方图）都近似同一条分布，相关性恒为 1，
    第二阶段形同虚设；pHash 碰撞因此没人把关，内容完全不同的两张图会被并成重复。
    """
    h, w = gray.shape
    blocks = max(1, min(int(blocks), min(h, w)))           # 图比分块还小时会切出空块，均值就是 NaN
    ys = np.linspace(0, h, blocks + 1).astype(int)
    xs = np.linspace(0, w, blocks + 1).astype(int)
    v = np.array([gray[ys[i]:ys[i + 1], xs[j]:xs[j + 1]].mean()
                  for i in range(blocks) for j in range(blocks)], dtype=np.float64)
    spread = float(v.max() - v.min())
    if spread <= 0:
        return np.array([float(v.mean())])         # 全平图没有布局信息，只剩整体亮度
    return (v - v.mean()) / spread


def _phash(gray: np.ndarray, size: int = 16) -> int:
    """平均哈希：缩到 16×16 后与均值比较，得 256 bit。

    自己做而不是引 imagehash：这只依赖 numpy 与已有的缩放，
    不额外增加一个必须联网安装的包。
    """
    from ..render import layout as L

    small = L.resize(gray.astype(np.float64), size, size, resample="area")
    bits = 0
    threshold = float(small.mean())
    for v in small.ravel():
        bits = (bits << 1) | int(v > threshold)
    return bits


def _hamming(a: int, b: int) -> int:
    x = a ^ b
    return int(x.bit_count()) if hasattr(int, "bit_count") else bin(x).count("1")


def laplacian_variance(gray: np.ndarray) -> float:
    kernel = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float64)
    from scipy import ndimage

    return float(ndimage.convolve(gray, kernel, mode="nearest").var())


# ---------------------------------------------------------------------- 判定算子

@register("dedup", "相似去重（pHash 粗筛 + 布局相关性确认）", {
    "distance": {"type": "integer", "default": 6, "min": 0, "max": 64,
                 "description": "汉明距离上限，越小越严格"},
    "content_corr": {"type": "number", "default": 0.98, "min": -1.0, "max": 1.0,
                     "description": "第二阶段：分块布局相关性下限，用来挡住 pHash 碰撞"},
    "keep": {"type": "string", "default": "first", "enum": ["first", "last", "sharpest"],
             "description": "一组重复里保留哪一个"},
})
class Dedup:
    name = "dedup"
    modalities = IMAGE_LIKE

    def __init__(self, **params):
        self.params = params
        for key, spec in CLEAN_OPS.get("dedup").schema.items():
            self.params.setdefault(key, spec["default"])

    def groups(self, items: Sequence, scores: Optional[dict] = None) -> list:
        """两阶段：先按 pHash 距离分桶，再用分块布局相关性确认。"""
        sigs = []
        for item in items:
            gray = _gray(item)
            sigs.append({"item": item, "hash": _phash(gray), "layout": _layout_sig(gray)})
        parent = list(range(len(sigs)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(sigs)):
            for j in range(i + 1, len(sigs)):
                if _hamming(sigs[i]["hash"], sigs[j]["hash"]) > int(self.params["distance"]):
                    continue
                if not self._confirm(sigs[i]["layout"], sigs[j]["layout"]):
                    continue
                parent[find(i)] = find(j)
        buckets: dict = {}
        for i, sig in enumerate(sigs):
            buckets.setdefault(find(i), []).append(sig)
        out = []
        for members in buckets.values():
            if len(members) < 2:
                continue
            if self.params["keep"] == "sharpest" and scores:
                members.sort(key=lambda s: -scores.get(str(s["item"].uri), 0.0))
            elif self.params["keep"] == "last":
                members.reverse()
            out.append([str(m["item"].uri) for m in members])
        return out

    def _confirm(self, a: np.ndarray, b: np.ndarray) -> bool:
        """布局相关。两边都是全平图时比亮度——那种图没有布局可言。"""
        if a.size == 1 or b.size == 1:
            return a.size == b.size and abs(float(a[0]) - float(b[0])) <= 1.0
        da, db = a - a.mean(), b - b.mean()
        denom = float(np.sqrt((da * da).sum() * (db * db).sum()))
        corr = float((da * db).sum() / denom) if denom > 0 else 1.0
        return corr >= float(self.params["content_corr"])

    def evaluate(self, items: Sequence, scores: Optional[dict] = None) -> list:
        groups = self.groups(items, scores)
        dropped = {uri for g in groups for uri in g[1:]}
        return [Decision(str(i.uri), self.name, keep=str(i.uri) not in dropped,
                         score=float(len(dropped)), reason="重复组内非保留者")
                for i in items]


@register("blur", "去模糊（Laplacian 方差，按显示口径归一后再算）", {
    "threshold": {"type": "number", "default": 40.0, "min": 0.0,
                  "description": "清晰度下限，低于此值判为模糊（0-255 尺度）"},
    "percentile": {"type": "number", "default": 0.0, "min": 0.0, "max": 99.0,
                   "description": "用全批清晰度分位数当阈值（>0 时忽略 threshold）"},
})
class Blur:
    name = "blur"
    modalities = IMAGE_LIKE

    def __init__(self, **params):
        self.params = params
        for key, spec in CLEAN_OPS.get("blur").schema.items():
            self.params.setdefault(key, spec["default"])

    def sharpness(self, item) -> float:
        """先按 0.5/99.5 分位拉到 0-255，再算 Laplacian 方差。

        直接用原始数值的话，同一张图在 FITS（计数可达几万）和 PNG（0-255）下差几个
        数量级，阈值根本没法给默认值；按显示口径归一之后，阈值才等于"看着清不清晰"。
        """
        from ..render.stretch import estimate_interval

        gray = _gray(item)
        lo, hi = estimate_interval(gray, "percent", percent=99.5)
        norm = np.clip((gray - lo) / (hi - lo), 0.0, 1.0) * 255.0
        return laplacian_variance(norm)

    def evaluate(self, items: Sequence, scores: Optional[dict] = None) -> list:
        values = {str(i.uri): self.sharpness(i) for i in items}
        cut = float(self.params["threshold"])
        pct = float(self.params["percentile"])
        if pct > 0 and values:
            cut = float(np.percentile(list(values.values()), pct))
        return [Decision(str(i.uri), self.name, keep=values[str(i.uri)] >= cut,
                         score=values[str(i.uri)],
                         reason="" if values[str(i.uri)] >= cut else f"清晰度 {values[str(i.uri)]:.1f} < {cut:.1f}")
                for i in items]


@register("content_filter", "内容过滤（交给后端判定，规则可换）", {
    "backend": {"type": "string", "default": "visual_prompt",
                "description": "用哪个后端给每一路打分"},
    "min_score": {"type": "number", "default": 0.6, "description": "保留的分数下限"},
    "rule": {"type": "string", "default": "keep_if_matched",
             "enum": ["keep_if_matched", "drop_if_matched"],
             "description": "命中即保留 / 命中即剔除（「没命中才留」就是 drop，不必再列一条）"},
    "threshold": {"type": "number", "default": 0.6, "description": "传给后端的相似度阈值"},
    "reference": {"type": "string", "default": "",
                  "description": "提示未带模板时，模板取自哪张图；留空用本批第一张"},
})
class ContentFilter:
    """把"内容过滤"泛化成 (后端, 提示, 规则)：

    原平台只支持"包含某物体就保留"，换一个后端和一条规则就能表达
    "有宇宙线就剔掉"或"没检测到耀斑就留着当背景"，逻辑只有一份。
    """

    name = "content_filter"
    modalities = IMAGE_LIKE

    def __init__(self, prompts: Sequence = (), **params):
        self.params = params
        for key, spec in CLEAN_OPS.get("content_filter").schema.items():
            self.params.setdefault(key, spec["default"])
        self.prompts = list(prompts)

    def anchored(self, items: Sequence) -> list:
        """把没有模板的提示钉到同一张参考图上。

        视觉后端在提示无模板时就地从"当前这张图"取模板，于是每张图都和自己匹配、
        分数恒为 1.0，内容过滤会把整批同进同出——先固定模板，分数才有区分力。
        """
        from ..annotate.prompts import resolve_prompt

        if not self.prompts or all(p.template is not None for p in self.prompts):
            return list(self.prompts)
        ref = str(self.params.get("reference") or "") or \
            (str(items[0].uri) if items else "")
        if not ref:
            return list(self.prompts)
        return [p if p.template is not None else
                resolve_prompt(p.source_uri or ref, p) for p in self.prompts]

    def evaluate(self, items: Sequence, scores: Optional[dict] = None) -> list:
        from ..annotate.prompts import score_items

        rule = str(self.params["rule"])
        if rule not in ("keep_if_matched", "drop_if_matched"):
            raise ValueError(f"未知过滤规则 {rule!r}，可用 keep_if_matched / drop_if_matched")
        if not items:
            return []
        backend = str(self.params["backend"])
        params = {"threshold": float(self.params["threshold"])} if backend == "visual_prompt" else {}
        ranked = {r["uri"]: r["score"] for r in score_items(
            [str(i.uri) for i in items], backend, self.anchored(items), params)}
        cut = float(self.params["min_score"])
        out = []
        for item in items:
            score = float(ranked.get(str(item.uri), 0.0))
            matched = score >= cut
            keep = matched if rule == "keep_if_matched" else not matched
            out.append(Decision(str(item.uri), self.name, keep=keep, score=score,
                                reason="" if keep else f"规则 {rule}，命中分数 {score:.2f}"))
        return out


@register("series_clean", "时序清洗（坏点率 / 冻结段 / 离散通道违规）", {
    "max_bad_ratio": {"type": "number", "default": 0.02, "description": "NaN/Inf 比例上限"},
    "frozen_run": {"type": "integer", "default": 120, "description": "连续完全相同的最长允许长度"},
    "check_discrete": {"type": "boolean", "default": True,
                       "description": "离散通道出现非登记值即判为脏"},
})
class SeriesClean:
    name = "series_clean"
    modalities = (Modality.SERIES.value,)

    def __init__(self, calibration=None, **params):
        self.params = params
        for key, spec in CLEAN_OPS.get("series_clean").schema.items():
            self.params.setdefault(key, spec["default"])
        self.calibration = calibration

    def evaluate(self, items: Sequence, scores: Optional[dict] = None) -> list:
        out = []
        for item in items:
            ts = item.load()
            values = np.asarray(ts.values, dtype=np.float64)
            bad = float(np.mean(~np.isfinite(values))) if values.size else 1.0
            problems = []
            if bad > float(self.params["max_bad_ratio"]):
                problems.append(f"坏点率 {bad:.3f}")
            run = self._longest_frozen(values)
            if run >= int(self.params["frozen_run"]):
                problems.append(f"冻结段 {run} 点")
            if self.params["check_discrete"]:
                violation = self._discrete_violation(ts, values)
                if violation:
                    problems.append(f"离散通道 {violation} 出现未登记值")
            out.append(Decision(str(item.uri), self.name, keep=not problems, score=bad,
                                reason="；".join(problems)))
        return out

    @staticmethod
    def _longest_frozen(values: np.ndarray) -> int:
        """至少一根通道连续不变的最大长度（忽略整列为 NaN 的情况）。"""
        if values.size == 0:
            return 0
        best = 0
        for col in range(values.shape[1]):
            v = values[:, col]
            finite = np.isfinite(v)
            if not finite.any():
                continue
            run = 1
            for i in range(1, v.size):
                if finite[i] and finite[i - 1] and v[i] == v[i - 1]:
                    run += 1
                    best = max(best, run)
                else:
                    run = 1
        return int(best)

    def _discrete_violation(self, ts, values) -> str:
        """离散通道只允许出现登记过的码值；连标定表都没有时，
        至少不许出现小数——出现小数说明中间被插值过，那是假的工况。"""
        table = {}
        if self.calibration is not None:
            for name, spec in (getattr(self.calibration, "table", None) or {}).items():
                if isinstance(spec, dict) and spec.get("values"):
                    table[name] = {float(k) for k in spec["values"]}
        for idx, discrete in enumerate(ts.discrete):
            if not discrete:
                continue
            name = ts.channels[idx]
            seen = {round(float(v), 6) for v in np.unique(values[:, idx]) if np.isfinite(v)}
            allowed = table.get(name)
            if allowed:
                if seen - allowed:
                    return name
            elif any(v != int(v) for v in seen):
                return name
        return ""


# ---------------------------------------------------------------------- 变换算子

@register("crop", "裁剪（等比或指定框）", {
    "box": {"type": "array", "default": None, "description": "[x0,y0,x1,y1]，缺省按 center/fraction 居中裁"},
    "fraction": {"type": "number", "default": 0.8, "min": 0.1, "max": 1.0,
                 "description": "保留的边长比例"},
})
class Crop:
    name = "crop"
    modalities = IMAGE_LIKE
    is_transform = True

    def __init__(self, **params):
        self.params = params
        for key, spec in CLEAN_OPS.get("crop").schema.items():
            self.params.setdefault(key, spec["default"])

    def box(self, shape: tuple) -> tuple:
        h, w = shape[-2], shape[-1]
        box = self.params.get("box")
        if box:
            x0, y0, x1, y1 = [int(round(v)) for v in box]
            return (max(0, min(x0, w)), max(0, min(y0, h)), max(1, min(x1, w)), max(1, min(y1, h)))
        frac = float(self.params["fraction"])
        nw, nh = max(1, int(round(w * frac))), max(1, int(round(h * frac)))
        return ((w - nw) // 2, (h - nh) // 2, (w - nw) // 2 + nw, (h - nh) // 2 + nh)

    def apply(self, arr: np.ndarray) -> np.ndarray:
        x0, y0, x1, y1 = self.box(arr.shape)
        return arr[..., y0:y1, x0:x1] if arr.ndim == 2 else arr[y0:y1, x0:x1]


@register("rotate", "旋转（90 的整数倍，不插值）", {
    "k": {"type": "integer", "default": 1, "description": "逆时针 90° 的次数（0-3）"},
})
class Rotate:
    name = "rotate"
    modalities = IMAGE_LIKE
    is_transform = True

    def __init__(self, **params):
        self.params = params
        for key, spec in CLEAN_OPS.get("rotate").schema.items():
            self.params.setdefault(key, spec["default"])

    def apply(self, arr: np.ndarray) -> np.ndarray:
        k = int(self.params["k"]) % 4
        return np.rot90(arr, k)                       # 90 倍只用转置，掩膜编号不会被插值污染


@register("mirror", "镜像（水平/垂直）", {
    "horizontal": {"type": "boolean", "default": True},
})
class Mirror:
    name = "mirror"
    modalities = IMAGE_LIKE
    is_transform = True

    def __init__(self, **params):
        self.params = params
        for key, spec in CLEAN_OPS.get("mirror").schema.items():
            self.params.setdefault(key, spec["default"])

    def apply(self, arr: np.ndarray) -> np.ndarray:
        return arr[:, ::-1] if self.params["horizontal"] else arr[::-1, :]


def _with_coords(ann, coords):
    return type(ann)(ann.geom, coords, ann.label, ann.source, ann.confidence,
                     ann.backend, ann.slice, ann.level, ann.color, ann.note)


def _as_box(geom: str, coords: list) -> list:
    """旋转后左上/右下会互换，矩形必须重新归一，否则 `Ann` 校验直接报错。"""
    if geom != "box":
        return coords
    return [min(coords[0], coords[2]), min(coords[1], coords[3]),
            max(coords[0], coords[2]), max(coords[1], coords[3])]


def map_geometry(ann, chain: Sequence[tuple], shape: tuple):
    """标注几何跟着变换链走一遍，返回 (新标注, 新 (h, w))。

    坐标约定与 `Ann.flipped` 一致（镜像关于 W/2，不减 1）：数组翻转按像素索引做，
    与连续坐标差半个像素，两处必须同一套数，否则界面和导出会整体偏一像素。
    空间变换对 interval/level 没有意义，时间平移（shift_time）对 box 没有意义，
    各自跳过自己不管的那一路——把时间区间当成矩形去旋转是最容易静默出错的组合。
    """
    h, w = float(shape[-2]), float(shape[-1])
    for name, params in chain:
        temporal = ann.geom in ("interval", "level")
        if name == "shift_time":
            if not temporal:
                continue
            dt = float(params["dt"])
            ann = _with_coords(ann, [c + dt for c in ann.coords])
            continue
        if temporal:
            break
        if name == "crop":
            known = {k: v for k, v in params.items() if k in ("box", "fraction")}
            x0, y0, x1, y1 = Crop(**known).box((int(h), int(w)))
            ann = _with_coords(ann, [c + (-x0 if i % 2 == 0 else -y0)
                                     for i, c in enumerate(ann.coords)])
            h, w = float(y1 - y0), float(x1 - x0)
        elif name == "rotate":
            for _ in range(int(params.get("k", 1)) % 4):
                pts = []
                for i in range(0, len(ann.coords), 2):
                    # np.rot90(m,1)[i,j] = m[j, W-1-i]，所以 (x,y) -> (y, W-x)。
                    # 这个方向是从 numpy 的实现倒推出来的，凭"逆时针"直觉写会反过来。
                    pts.extend([ann.coords[i + 1], w - ann.coords[i]])
                ann = _with_coords(ann, _as_box(ann.geom, pts))
                h, w = w, h
        elif name == "mirror":
            ann = ann.flipped(bool(params.get("horizontal", True)), width=w, height=h)
        else:
            raise ValueError(f"{name!r} 不是几何变换，无法映射标注")
    return ann, (h, w)


def write_manifest(path: Path, records: Iterable[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"files": list(records)}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def digest(path) -> str:
    h = hashlib.sha256()
    with open(str(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


__all__ = ["Blur", "CLEAN_OPS", "ContentFilter", "Crop", "Decision", "Dedup", "Mirror",
           "Rotate", "SeriesClean", "TRANSFORMS", "digest", "laplacian_variance",
           "make_register_func", "map_geometry", "register", "write_manifest"]
