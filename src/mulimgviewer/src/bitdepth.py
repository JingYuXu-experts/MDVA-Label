"""位深归一化。

单独成模块是为了可测试：utils_img.py 依赖 wx 与 reportlab，在没有图形
环境的机器上根本 import 不动，而这段逻辑是纯数值计算，不该被那些依赖拖累。
"""

import numpy as np


def to_uint8_display(img):
    """把任意位深的图像映射到 8 位显示范围。

    整数类型按该位深的理论量程线性拉伸，而不是按当前图的最小最大值：
    多图对比工具里每张图各自拉伸会让两张亮度不同的图看起来一样，
    比较结果就失去意义了。浮点类型没有理论量程可依据，退回实际 min-max。
    """
    arr = np.asarray(img)
    if arr.dtype == np.uint8:
        return arr

    if arr.dtype.kind == "f":
        finite = arr[np.isfinite(arr)]
        if finite.size == 0:
            return np.zeros(arr.shape, dtype=np.uint8)
        lo, hi = float(finite.min()), float(finite.max())
    elif arr.dtype.kind in ("u", "i"):
        info = np.iinfo(arr.dtype)
        lo, hi = float(info.min), float(info.max)
    elif arr.dtype.kind == "b":
        return (arr.astype(np.uint8)) * 255
    else:
        raise TypeError(f"不支持的位深类型: {arr.dtype}")

    if hi <= lo:
        return np.zeros(arr.shape, dtype=np.uint8)

    scaled = (arr.astype(np.float64) - lo) * (255.0 / (hi - lo))
    return np.clip(np.nan_to_num(scaled, nan=0.0, posinf=255.0, neginf=0.0), 0, 255).astype(np.uint8)


def bit_depth_of(arr) -> int:
    kind = arr.dtype.kind
    if kind == "b":
        return 1
    return int(np.dtype(arr.dtype).itemsize * 8)
