"""视觉提示后端：用经典模板匹配实现"框一个 → 找同类"。

用户决定不接大模型，所以 T-Rex 那套"点/框/涂鸦提示 + 开集检测"在这里换成
纯 CPU 的实现：归一化互相关（FFT 加速）找候选，再用局部对比度、灰度直方图
相关性和通道均值差过滤。它能做同图找相似与跨图传播，但**不做开集语义识别**
——论文措辞按这个边界写，不宣称深度检测能力。
"""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from ..analysis.base import to_float, to_gray
from ..annotate.model import Ann
from ..core.model import DataItem, Slice
from ..render.image import pick_slice
from .base import Backend, BackendManifest, Prompt, register

EPS = 1e-9

SCHEMA = {
    "threshold": {"type": "number", "default": 0.62, "min": 0.2, "max": 0.98,
                  "description": "相似度下限（NCC）"},
    "max_matches": {"type": "integer", "default": 40, "description": "每张最多返回多少个提示结果"},
    "min_sep": {"type": "number", "default": 6.0, "description": "两个命中中心的最小距离（像素）"},
    "hist_tol": {"type": "number", "default": 0.75, "description": "灰度直方图相关性下限"},
    "min_contrast": {"type": "number", "default": 1.5, "description": "局部标准差下限，滤掉平坦区"},
    "point_size": {"type": "integer", "default": 15, "description": "点提示时自动取的正方形边长"},
    "use_color": {"type": "boolean", "default": True, "description": "彩色图额外比较三通道均值"},
    "color_tol": {"type": "number", "default": 0.35,
                  "description": "通道均值相对偏差上限，超过判为不同颜色（伪彩图不受影响）"},
}


def ncc_map(img: np.ndarray, tpl: np.ndarray) -> np.ndarray:
    """归一化互相关图。用 FFT 相关而不是逐窗循环，512×512 配 30×30 模板是毫秒级。"""
    from scipy import ndimage, signal

    kh, kw = tpl.shape
    h, w = img.shape
    if kh > h or kw > w:
        raise ValueError(f"模板 {tpl.shape} 不小于图像 {img.shape}，请缩小提示框")
    tpl0 = tpl - tpl.mean()
    std_t = float(tpl0.std())
    n = kh * kw
    cross = signal.correlate(img, tpl0, mode="valid", method="fft") / n
    y0, x0 = kh // 2, kw // 2
    local_mean = ndimage.uniform_filter(img, size=(kh, kw), mode="constant")[
        y0:y0 + cross.shape[0], x0:x0 + cross.shape[1]]
    local_sq = ndimage.uniform_filter(img * img, size=(kh, kw), mode="constant")[
        y0:y0 + cross.shape[0], x0:x0 + cross.shape[1]]
    var = np.clip(local_sq - local_mean ** 2, 0.0, None)
    return cross / (np.sqrt(var) * std_t + EPS)


def _hist_corr(a: np.ndarray, b: np.ndarray, bins: int = 16) -> float:
    lo = min(a.min(), b.min())
    hi = max(a.max(), b.max())
    if hi - lo <= 0:
        return 1.0
    ha, _ = np.histogram(a, bins=bins, range=(lo, hi))
    hb, _ = np.histogram(b, bins=bins, range=(lo, hi))
    ha = ha.astype(np.float64) / max(1.0, ha.sum())
    hb = hb.astype(np.float64) / max(1.0, hb.sum())
    da, db = ha - ha.mean(), hb - hb.mean()
    denom = float(np.sqrt((da * da).sum() * (db * db).sum()))
    return float((da * db).sum() / denom) if denom > EPS else 1.0


def _crop(arr: np.ndarray, box: Sequence[float]) -> np.ndarray:
    h, w = arr.shape[:2]
    y0 = int(max(0, min(round(box[1]), h - 1)))
    x0 = int(max(0, min(round(box[0]), w - 1)))
    y1 = int(max(y0 + 1, min(round(box[3]), h)))
    x1 = int(max(x0 + 1, min(round(box[2]), w)))
    return arr[y0:y1, x0:x1]


def _prompt_box(prompt: Prompt, shape: tuple, point_size: int = 15) -> list:
    h, w = shape[:2]
    if prompt.kind == "box":
        x0, y0, x1, y1 = prompt.coords
    elif prompt.kind == "point":
        cx, cy = prompt.coords[0], prompt.coords[1]
        half = max(2, int(point_size) // 2)
        x0, y0, x1, y1 = cx - half, cy - half, cx + half, cy + half
    else:                                   # poly：用外接框
        xs, ys = prompt.coords[0::2], prompt.coords[1::2]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    return [max(0.0, min(x0, w - 1)), max(0.0, min(y0, h - 1)),
            max(1.0, min(x1, w)), max(1.0, min(y1, h))]


def template_of(item: DataItem, prompt: Prompt, window: Optional[Slice] = None,
                point_size: int = 15) -> np.ndarray:
    """从提示里取模板像素（灰度）。带 source_uri 的传播提示应已自带 template。"""
    patch, _rgb = _template_pair(item, prompt, window, point_size)
    return patch


def _template_pair(item, prompt, window, point_size) -> tuple:
    """同时给出灰度模板与彩色模板：彩色只用于通道均值过滤，不参与相关计算。"""
    if prompt.template is not None:
        raw = to_float(prompt.template)
        rgb = raw if raw.ndim == 3 else None
        gray = to_gray(raw)
        return gray, rgb
    arr = pick_slice(item, window or Slice.full())
    box = _prompt_box(prompt, arr.shape, point_size)
    crop = _crop(to_float(arr), box)
    if crop.size == 0:
        raise ValueError("提示落在图像之外，取不到模板")
    return to_gray(crop), (crop if crop.ndim == 3 else None)


@register("visual_prompt", "视觉提示（模板匹配 + 一致性过滤，纯 CPU）", SCHEMA)
class VisualPromptBackend(Backend):
    manifest = BackendManifest(
        name="visual_prompt",
        title="视觉提示·模板匹配",
        family="visual_prompt",
        tasks=("detect_similar", "propagate", "content_filter"),
        modalities=("image", "cube", "video"),
        domains=("*",),
        schema=SCHEMA,
        supports_prompt=True,
        prompt_kinds=("box", "point", "poly"),
        note="不做开集语义识别：它找的是「长得像这一块」的位置，不是「这是耀斑」。",
    )

    def available(self, probe=None) -> tuple:
        import importlib.util

        if importlib.util.find_spec("scipy") is None:
            return (False, "缺少 scipy.signal，模板匹配不可用（pip install -r requirements/science.txt）")
        return (True, "")

    def predict(self, item: DataItem, prompts: Sequence[Prompt] = (),
                window: Optional[Slice] = None) -> list:
        if not prompts:
            return []
        sl = window or Slice.full()
        arr = pick_slice(item, sl)
        gray = to_gray(arr)
        color = to_float(arr) if arr.ndim == 3 else None
        index = int(sl.index or 0) if item.n_views > 1 else None

        anns: list = []
        for prompt in prompts:
            tpl, tpl_rgb = _template_pair(item, prompt, sl, int(self.params["point_size"]))
            if tpl_rgb is None and color is not None:
                box = _prompt_box(prompt, arr.shape, int(self.params["point_size"]))
                tpl_rgb = _crop(color, box)
            anns.extend(self._match_one(gray, tpl, tpl_rgb, prompt, index, color))
        return anns

    # ------------------------------------------------------------------ 内部

    def _match_one(self, gray, tpl, tpl_rgb, prompt, index, color) -> list:
        thr = float(self.params["threshold"])
        resp = ncc_map(gray, tpl)
        kh, kw = tpl.shape
        flat = np.argsort(resp.ravel())[::-1]
        taken: list = []
        min_sep = float(self.params["min_sep"])
        contrast = float(self.params["min_contrast"])
        hist_tol = float(self.params["hist_tol"])
        limit = int(self.params["max_matches"])
        want_color = bool(self.params["use_color"]) and color is not None and tpl_rgb is not None
        ref = tpl_rgb.reshape(-1, tpl_rgb.shape[-1]).mean(axis=0) if want_color else None

        for cell in flat:
            if len(taken) >= limit:
                break
            fy, fx = divmod(int(cell), resp.shape[1])
            score = float(resp[fy, fx])
            if score < thr:
                break                     # 降序遍历，后面只会更低
            cy, cx = fy + kh / 2.0, fx + kw / 2.0
            if any(abs(cy - py) < min_sep and abs(cx - px) < min_sep for px, py, _ in taken):
                continue
            patch = gray[fy:fy + kh, fx:fx + kw]
            if float(patch.std()) < contrast or _hist_corr(tpl, patch) < hist_tol:
                continue
            if want_color and not self._color_ok(color[fy:fy + kh, fx:fx + kw], ref):
                continue
            taken.append((cx, cy, score))

        out = []
        for cx, cy, score in taken:
            out.append(Ann("box", [cx - kw / 2.0, cy - kh / 2.0, cx + kw / 2.0, cy + kh / 2.0],
                           prompt.label, source="visual_prompt",
                           confidence=round(float(np.clip(score, 0.0, 1.0)), 4),
                           backend="visual_prompt", slice=index))
        return out

    def _color_ok(self, patch3, ref) -> bool:
        """通道均值相对偏差：伪彩图是单通道扩色，rgb 三通道相同，偏差恒为 0，不会被误杀。"""
        if patch3.ndim != 3 or patch3.shape[-1] < 3 or ref is None:
            return True
        got = np.asarray(patch3[..., :3], dtype=np.float64).reshape(-1, 3).mean(axis=0)
        scale = float(np.abs(ref).mean()) or 1.0
        dev = float(np.abs(got - ref).max() / scale)
        return dev <= float(self.params["color_tol"])

    def score_for(self, item: DataItem, prompt: Prompt,
                  window: Optional[Slice] = None) -> float:
        """内容过滤用的"这一路里有没有提示目标"——取最高相似度，不返回位置。"""
        anns = self.predict(item, [prompt], window)
        return max([a.confidence for a in anns], default=0.0)

    def find_similar(self, item: DataItem, prompt: Prompt,
                     window: Optional[Slice] = None) -> list:
        """同图找相似：画布上框一个，其余同类目标自动补齐。"""
        return self.predict(item, [prompt], window)

    @staticmethod
    def as_prompt(ann: Ann, source_uri: str = "") -> Prompt:
        if ann.geom not in ("box", "point", "poly"):
            raise ValueError(f"{ann.geom} 几何不能当视觉提示")
        return Prompt(ann.geom, list(ann.coords), ann.label, "positive", source_uri)


def supports(item: DataItem) -> bool:
    return item.modality in VisualPromptBackend.manifest.modalities


__all__ = ["VisualPromptBackend", "ncc_map", "template_of", "supports"]
