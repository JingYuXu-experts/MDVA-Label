"""字体解析：PIL 不指定字体时用的是内置位图字体，里面没有汉字。

渲染层所有标题、坐标轴刻度、标注文字都从这里取字体，否则中文标签一律
画成方框——对一个中文界面的科研工具来说这是硬伤。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

__all__ = ["font_path", "text_font"]

# 按平台顺序尝试；命中第一个就够用
_CANDIDATES = (
    "msyh.ttc",
    "simhei.ttf",
    "simsun.ttc",
    "Deng.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
)


@lru_cache(maxsize=1)
def font_path() -> Optional[str]:
    """找一个能画汉字的字体文件。`MDVA_FONT` 显式指定优先。"""
    env = os.environ.get("MDVA_FONT")
    if env:
        return env if Path(env).is_file() else None

    win_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for cand in _CANDIDATES:
        path = Path(cand)
        if path.is_absolute():
            if path.is_file():
                return str(path)
        elif (win_fonts / cand).is_file():
            return str(win_fonts / cand)
    return None


@lru_cache(maxsize=32)
def text_font(size: int = 12):
    from PIL import ImageFont

    path = font_path()
    if path:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 的 load_default 不接受字号
        return ImageFont.load_default()
