"""L2 布局：把多个已渲染位图拼成网格总图。

移植自上游 utils_img.get_xy_grid / layout_2d 的空间坐标计算思路，
但去掉对 PIL.Image 对象的依赖，只处理 ndarray，便于在服务端与
测试里直接调用。
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np

from ..core.font import text_font


def grid_position(index: int, rows: int, cols: int, cell_w: int, cell_h: int,
                  gap_x: int = 0, gap_y: int = 0, outer_x: int = 0, outer_y: int = 0):
    """第 index 个格子的左上角坐标与尺寸。"""
    r, c = divmod(index, cols)
    x = outer_x + c * (cell_w + gap_x)
    y = outer_y + r * (cell_h + gap_y)
    return x, y, cell_w, cell_h


def auto_grid(n: int, max_side: int = 8, aspect: float = 1.0) -> tuple:
    """按数量推断行列，使整体尽量接近给定长宽比。"""
    if n <= 0:
        return (0, 0)
    if n == 1:
        return (1, 1)
    cols = max(1, min(max_side, int(math.ceil(math.sqrt(n * aspect)))))
    rows = max(1, int(math.ceil(n / cols)))
    return (rows, cols)


def mosaic(tiles: Sequence[np.ndarray], rows: int, cols: int, gap: int = 4,
           bg=(24, 24, 28), pad: int = 0, labels: Optional[Sequence[str]] = None,
           label_height: int = 18) -> np.ndarray:
    """把 tiles 拼成 rows x cols 总图。tile 尺寸不一致时按比例缩放到统一格心。"""
    tiles = [t for t in tiles if t is not None and t.size]
    if not tiles:
        return np.zeros((1, 1, 3), dtype=np.uint8)

    cell_h, cell_w = tiles[0].shape[0], tiles[0].shape[1]
    if labels:
        cell_h += label_height

    width = cols * cell_w + max(0, cols - 1) * gap + 2 * pad
    height = rows * cell_h + max(0, rows - 1) * gap + 2 * pad
    canvas = np.empty((height, width, 3), dtype=np.uint8)
    canvas[:] = bg

    from PIL import Image, ImageDraw

    pil_canvas = Image.fromarray(canvas)
    draw = ImageDraw.Draw(pil_canvas)

    for i, tile in enumerate(tiles[: rows * cols]):
        r, c = divmod(i, cols)
        x = pad + c * (cell_w + gap)
        y = pad + r * (cell_h + gap)
        img = tile
        if img.shape[1] != cell_w or img.shape[0] != (cell_h - (label_height if labels else 0)):
            img = resize(img, cell_w, cell_h - (label_height if labels else 0))
        pil_canvas.paste(Image.fromarray(img), (x, y + (label_height if labels else 0)))
        if labels and i < len(labels) and labels[i]:
            draw.text((x + 3, y + 2), str(labels[i])[:60], fill=(200, 200, 200),
                      font=text_font(12))

    return np.asarray(pil_canvas)


def resize(arr: np.ndarray, width: int, height: int, resample: str = "bilinear") -> np.ndarray:
    """缩放。标签图/掩膜必须传 resample="nearest"，见 render_labels。"""
    from PIL import Image

    mode = Image.NEAREST if resample == "nearest" else Image.BILINEAR
    img = Image.fromarray(arr)
    return np.asarray(img.resize((width, height), mode))


def crop_view(arr: np.ndarray, box) -> np.ndarray:
    """按 (x0, y0, x1, y1) 裁剪，用于放大框。"""
    x0, y0, x1, y1 = (int(v) for v in box)
    h, w = arr.shape[:2]
    x0, x1 = max(0, min(x0, w)), max(0, min(x1, w))
    y0, y1 = max(0, min(y0, h)), max(0, min(y1, h))
    if x1 <= x0 or y1 <= y0:
        return arr[:0]
    return arr[y0:y1, x0:x1]
