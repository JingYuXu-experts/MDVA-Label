"""L2 统一渲染入口：DataItem -> RGB。

所有模态在这里被归一为同一件事——"按 Slice 取一段内容，画进一个格心"，
因此网格拼接、并行放大、导出总图对四种模态一视同仁。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence, Tuple

import numpy as np

from ..core.model import DataItem, Modality, Slice
from ..core.timefmt import fmt_axis
from . import layout as L
from . import overlay as OV
from . import stretch as S


def _is_frame_source(obj) -> bool:
    """帧序列后端。用鸭子类型判断，避免 render 反向依赖 io。"""
    return hasattr(obj, "frame") and hasattr(obj, "n_frames")


def _kw_names(func) -> frozenset:
    import inspect

    return frozenset(p.name for p in inspect.signature(func).parameters.values()
                     if p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD))


def _view_index(item: DataItem, window: Slice, n_views: int) -> int:
    """把 Slice 折算成切片序号：有时间基准就按时间取，否则按序号。"""
    last = max(0, n_views - 1)
    if item.modality == Modality.VIDEO.value and np.isfinite(window.t0) and item.t_ref is not None:
        fps = float(item.meta.get("fps") or 0)
        if fps > 0:
            return int(np.clip(round((window.t0 - item.t_ref) * fps), 0, last))
    return int(np.clip(window.index, 0, last))


def pick_slice(item: DataItem, window: Slice) -> np.ndarray:
    """取立方体或视频的一帧；越界索引夹到合法范围而不是报错。"""
    arr = item.load()
    if _is_frame_source(arr):
        return np.asarray(arr.frame(_view_index(item, window, arr.n_frames)))
    if arr.ndim <= 2:
        return arr
    if item.n_views <= 1:
        # 单帧彩色图是 (h, w, 3)，按第 0 轴切就把一张图切成了三通道的"两帧"
        return arr
    last = arr.shape[0] - 1
    if np.isfinite(window.t0) and item.n_views > 1:
        # 时间驱动的切片：按数据时间跨度线性映射到切片序号
        a, b = item.time_span
        if np.isfinite(b) and b > a:
            frac = (window.t0 - a) / (b - a)
            return arr[int(np.clip(round(frac * last), 0, last))]
    return arr[int(np.clip(window.index, 0, last))]


@dataclass
class RenderResult:
    rgb: np.ndarray
    title: str = ""
    subtitle: str = ""
    info: dict = field(default_factory=dict)
    overlay: Optional["OV.OverlayData"] = None

    def composited(self) -> np.ndarray:
        """叠加标注后的最终位图。"""
        if self.overlay is None or self.overlay.is_empty():
            return self.rgb
        return OV.draw_overlay(self.rgb, self.overlay)


def fit_size(src_h: int, src_w: int, box_w: int, box_h: int) -> Tuple[int, int]:
    """等比缩放目标尺寸，保持像元长宽比不变——科学图像不能各向异性拉伸。"""
    if src_w <= 0 or src_h <= 0:
        return (max(1, box_w), max(1, box_h))
    scale = min(box_w / src_w, box_h / src_h)
    return (max(1, int(round(src_w * scale))), max(1, int(round(src_h * scale))))


def _looks_like_color(arr: np.ndarray) -> bool:
    """最后一个轴是 3/4 通道即视为彩色，而非立方体的第 0 轴切片。"""
    return arr.ndim == 3 and arr.shape[-1] in (3, 4)


def _fit_canvas(rgb: np.ndarray, size: tuple, keep_aspect: bool = True,
                resample: str = "bilinear"):
    """等比贴进画布并返回实际落位，供三种渲染路径共用。"""
    box_w, box_h = int(size[0]), int(size[1])
    if not keep_aspect:
        return L.resize(rgb, box_w, box_h, resample=resample), None
    w, h = fit_size(rgb.shape[0], rgb.shape[1], box_w, box_h)
    scaled = L.resize(rgb, w, h, resample=resample)
    canvas = np.full((box_h, box_w, 3), 16, dtype=np.uint8)
    y0 = (box_h - h) // 2
    x0 = (box_w - w) // 2
    canvas[y0:y0 + h, x0:x0 + w] = scaled[:, :, :3]
    fit = (x0, y0, w, h, min(box_w / rgb.shape[1], box_h / rgb.shape[0]))
    return canvas, fit


def render_color(arr: np.ndarray, size: tuple, vmin=None, vmax=None,
                 keep_aspect: bool = True) -> Tuple[np.ndarray, dict]:
    """彩色数组直接归一化，不走伪彩 LUT。

    三个通道共用同一组上下界：逐通道各自拉伸会改变颜色平衡，
    病理图与显微图上的色差本身就是要比较的信息，不能被抹掉。
    """
    a = np.asarray(arr, dtype=np.float64)
    finite = a[np.isfinite(a)]
    if vmin is None or vmax is None:
        vmin = float(finite.min()) if finite.size else 0.0
        vmax = float(finite.max()) if finite.size else 1.0
    if not vmax > vmin:
        span = abs(vmax) * 0.05 or 1.0
        vmin, vmax = vmin - span / 2, vmax + span / 2
    norm = np.clip((a - vmin) / (vmax - vmin), 0.0, 1.0)
    rgb = (norm * 255.0).astype(np.uint8)
    info = {"vmin": float(vmin), "vmax": float(vmax), "interval": "shared",
            "stretch": "linear", "colormap": "native",
            "bad_pixels": int(a.size - finite.size)}

    canvas, fit = _fit_canvas(rgb, size, keep_aspect)
    if fit is not None:
        info["fit"] = fit
        info["pixel_scale"] = fit[-1]
    return canvas, info


def render_labels(arr: np.ndarray, palette: Sequence[Sequence[int]], size: tuple,
                  keep_aspect: bool = True) -> Tuple[np.ndarray, dict]:
    """调色板模式（分割掩膜、标签图）按类别配色。

    两件事不能错：缩放必须用最近邻，双线性会在两个类别的边界上插出第三种
    根本不存在的标签值；越界类别画成品红而不是静默的背景黑，否则漏画的类
    看起来就像背景。
    """
    idx = np.asarray(arr)
    n_class = len(palette or [])
    lut = np.full((256, 3), (255, 0, 255), dtype=np.uint8)
    for i, color in enumerate(list(palette or [])[:256]):
        lut[i] = tuple(color)[:3]
    flat = np.clip(idx.astype(np.int64), 0, 255)
    rgb = lut[flat]
    info = {
        "vmin": 0, "vmax": max(n_class - 1, 1), "interval": "labels",
        "stretch": "nearest", "colormap": "palette", "n_classes": n_class,
        "out_of_range": int((idx > n_class - 1).sum()) if idx.size else 0,
    }
    canvas, fit = _fit_canvas(rgb, size, keep_aspect, resample="nearest")
    if fit is not None:
        info["fit"] = fit
        info["pixel_scale"] = fit[-1]
    return canvas, info


def render_array(
    arr: np.ndarray,
    size: Tuple[int, int] = (512, 512),
    interval: str = "zscale",
    stretch: str = "linear",
    colormap: str = "gray",
    gamma: float = 1.0,
    vmin: Optional[float] = None,
    vmax: Optional[float] = None,
    invert: bool = False,
    keep_aspect: bool = True,
    palette: Optional[Sequence[Sequence[int]]] = None,
) -> Tuple[np.ndarray, dict]:
    if palette and arr.ndim == 2:
        return render_labels(arr, palette, size, keep_aspect=keep_aspect)
    if _looks_like_color(arr):
        return render_color(arr, size, vmin=vmin, vmax=vmax, keep_aspect=keep_aspect)

    rgb, info = S.to_rgb(
        arr, interval=interval, stretch=stretch, colormap=colormap,
        gamma=gamma, vmin=vmin, vmax=vmax, invert=invert,
    )
    canvas, fit = _fit_canvas(rgb, size, keep_aspect)
    if fit is None:
        return canvas, info
    info["fit"] = fit
    info["pixel_scale"] = fit[-1]
    return canvas, info


#: 空间分支只认 render_array 形参表里的键（见 render_item 的 §2.7 忽略口径）
_ARRAY_KEYS = _kw_names(render_array)


def render_item(
    item: DataItem,
    size: Tuple[int, int] = (512, 512),
    window: Optional[Slice] = None,
    style: Optional[dict] = None,
    overlay: Optional["OV.OverlayData"] = None,
    cursor_t: Optional[float] = None,
) -> RenderResult:
    sl = window or Slice.full()
    style = dict(style or {})
    title = item.title or item.id

    if item.modality in (Modality.IMAGE.value, Modality.CUBE.value, Modality.VIDEO.value):
        arr = pick_slice(item, sl)
        style.setdefault("palette", item.meta.get("palette"))
        # 契约 §2.7 的 23 个帧键是全模态共用参数面：channel/method/ymin/ymax/
        # out_width 这些时序专属键，空间模态收到就静默忽略，而不是透传进
        # render_array 抛 TypeError 变 500——前端多发一个键不该让整幅帧黑屏。
        style = {k: v for k, v in style.items() if k in _ARRAY_KEYS}
        rgb, info = render_array(arr, size, **style)
        subtitle = _array_subtitle(item, info)
        if item.modality == Modality.VIDEO.value:
            subtitle = f"帧 {sl.index}/{item.n_views}  " + subtitle
        return RenderResult(rgb=rgb, title=title, subtitle=subtitle, info=info, overlay=overlay)

    if item.modality == Modality.SERIES.value:
        from .series import render_channel

        ts = item.load()
        ch = int(sl.index) if sl.index else style.pop("channel", 0)
        ch = min(ch, ts.n_channels - 1)
        method = style.pop("method", "minmax")
        style.pop("channel", None)
        rgb, info = render_channel(
            ts, channel=ch, size=size, window=sl, method=method,
            discrete=ts.is_discrete(ch), cursor_t=cursor_t,
            title=f"{ts.channels[ch]} {ts.units[ch]}".strip(),
        )
        subtitle = _series_subtitle(item, info)
        return RenderResult(rgb=rgb, title=title, subtitle=subtitle, info=info, overlay=overlay)

    if item.modality == Modality.EVENT.value:
        events = item.load()
        t0, t1 = _event_bounds(events, sl)
        shown = events.select(sl)
        # 视图 0 是全部等级，之后按等级逐级查看（等级序号不连续，只能按位次取）
        levels = events.levels()
        level = None
        if sl.index > 0 and levels:
            level = levels[min(sl.index - 1, len(levels) - 1)]
            shown = shown.by_level(level)
        ivs = [OV.VInterval(t0=r.t, t1=r.t_end, label=r.label, level=r.level)
               for r in shown.rows]
        rgb = OV.render_intervals(size[0], size[1], t0, t1, ivs)
        subtitle = f"{len(shown.rows)}/{len(events.rows)} 条事件"
        if level is not None:
            subtitle += f"  等级 {level}"
        subtitle += f"  {fmt_axis(t0, t1 - t0)} ~ {fmt_axis(t1, t1 - t0)}"
        return RenderResult(
            rgb=rgb, title=title, subtitle=subtitle,
            info={"t0": t0, "t1": t1, "level": level, "n_shown": len(shown.rows)},
            overlay=overlay,
        )

    raise ValueError(f"无法渲染模态: {item.modality}")


def _event_bounds(events, sl: Slice) -> Tuple[float, float]:
    """有限窗口就按窗口画，否则事件轴永远铺满全程，联动放大等于没放大。"""
    if np.isfinite(sl.t0) and np.isfinite(sl.t1) and sl.t1 > sl.t0:
        return float(sl.t0), float(sl.t1)
    if events.rows and events.end > events.start:
        return float(events.start), float(events.end)
    return 0.0, 1.0


def _array_subtitle(item: DataItem, info: dict) -> str:
    if info.get("colormap") == "palette":
        parts = [f"类别 {info.get('n_classes', 0)}", f"{item.shape[-1]}x{item.shape[-2]}"]
        if info.get("out_of_range"):
            parts.append(f"越界标签 {info['out_of_range']}")
        return "  ".join(parts)
    rng = f"{info.get('vmin', 0):.4g} ~ {info.get('vmax', 1):.4g}"
    unit = item.meta.get("unit") or ""
    bad = info.get("bad_pixels", 0)
    parts = [f"[{rng}] {unit}".strip(), f"{item.shape[-1]}x{item.shape[-2]}"]
    if bad:
        parts.append(f"坏像元 {bad}")
    return "  ".join(parts)


def _series_subtitle(item: DataItem, info: dict) -> str:
    unit = item.meta.get("units", [""])[info.get("channel", 0)] if item.meta.get("units") else ""
    n = info.get("n_samples", 0)
    return f"{n} 点" + (f"  {unit}" if unit else "")


def render_mosaic(
    results: Sequence[RenderResult],
    rows: int, cols: int,
    cell: Tuple[int, int] = (512, 320),
    gap: int = 4,
    pad: int = 4,
    show_title: bool = True,
) -> np.ndarray:
    """并行对比总图：多路数据一次画进一张大图。

    这是"多模态并行可视化"的最终产物，也是导出论文对比图的来源。
    """
    label_h = 30 if show_title else 0
    box = (cell[0], cell[1] + label_h)
    tiles = [_label(r, box[0], box[1], cell[1]) for r in results]
    return L.mosaic(tiles, rows=rows, cols=cols, gap=gap, pad=pad)


def _label(result: RenderResult, width: int, height: int, img_h: int) -> np.ndarray:
    from PIL import Image, ImageDraw

    from ..core.font import text_font

    canvas = Image.new("RGB", (width, height), (24, 24, 28))
    draw = ImageDraw.Draw(canvas)
    img = result.composited()
    w, h = fit_size(img.shape[0], img.shape[1], width, img_h)
    img = L.resize(img, w, h)
    canvas.paste(Image.fromarray(img), ((width - w) // 2, height - img_h + (img_h - h) // 2))
    if result.title:
        draw.text((4, 1), result.title[:60], fill=(210, 214, 224), font=text_font(12))
    if result.subtitle:
        draw.text((4, 15), result.subtitle[:76], fill=(140, 148, 164), font=text_font(11))
    return np.asarray(canvas)


def to_uint8(arr: np.ndarray) -> np.ndarray:
    """线性拉伸到 uint8，NaN/Inf 当最低值而不是把它吞成报错。

    清洗与增强的落盘走这一个函数：所有通道共用同一组 min/max，
    逐通道各拉伸会把 RGB 的颜色比例拆散，界面里看到的颜色就不是原图的颜色了。
    """
    a = np.asarray(arr, dtype=np.float64)
    finite = a[np.isfinite(a)]
    if finite.size == 0:
        return np.zeros(a.shape, dtype=np.uint8)
    lo, hi = float(finite.min()), float(finite.max())
    if hi - lo <= 0:
        return np.full(a.shape, 0, dtype=np.uint8)
    scaled = np.clip((a - lo) / (hi - lo) * 255.0, 0, 255)
    return np.where(np.isfinite(a), scaled, 0.0).astype(np.uint8)


def save_array(path, arr: np.ndarray, stretch: bool = True) -> dict:
    """把任意位深的数组写成一张 PNG（图像/掩膜/立方体单帧通用），返回尺寸信息。

    `stretch=False` 是掩膜通道：类别编号是标签不是强度，0/1/2 拉伸成 0/127/255
    之后训练侧读到的就是三个谁也不认识的类，所以这里原样取整、越界直接报错。
    """
    from PIL import Image

    a = np.asarray(arr)
    if a.ndim == 3 and a.shape[-1] == 1:
        a = a[..., 0]
    if a.ndim == 3 and a.shape[-1] not in (3, 4):
        a = a[0]                                   # (C,H,W) 的立方体帧取第一通道，界面与导出同一帧
    if stretch:
        out = to_uint8(a)
    else:
        finite = a[np.isfinite(a)] if a.dtype.kind in ("f", "c") else a
        if finite.size and int(np.max(finite)) > 255:
            raise ValueError(f"掩膜类别编号超出 uint8 范围（最大 {int(np.max(finite))}）")
        out = np.rint(np.where(np.isfinite(a), a, 0.0)).astype(np.uint8) if a.dtype.kind == "f" \
            else a.astype(np.uint8)
    img = Image.fromarray(np.ascontiguousarray(out))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path))
    return {"file": str(path), "width": int(img.width), "height": int(img.height),
            "mode": img.mode, "bytes": Path(path).stat().st_size}
