"""图像 / 科学图像 / FITS 加载。

两条设计约束：
1. 惰性加载。扫描目录或批量跑流水线时，只为真正要显示的项付读数据的代价，
   因此加载器先探测 shape/dtype/时间戳，把实际读取留给 DataItem.load()。
2. 保位深。科学数据常见 uint16 / int32 / float32，必须把真实动态范围记进
   meta 交给渲染层拉伸；直接乘系数转 uint8 会让高位分量溢出回绕。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from ..core.model import DataItem, Modality
from .timeutil import to_utc_seconds

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")
SCIENTIFIC_IMAGE_SUFFIXES = (".tif", ".tiff")
FITS_SUFFIXES = (".fits", ".fit", ".fts")

_MJD_UNIX_EPOCH = 40587.0  # 1970-01-01T00:00:00UT 对应的 MJD


def _modality_for(ndim: int) -> str:
    return Modality.IMAGE.value if ndim <= 2 else Modality.CUBE.value


def _n_views_for(shape: tuple) -> int:
    return int(shape[0]) if len(shape) >= 3 else 1


def fits_mjd_to_utc(mjd) -> Optional[float]:
    try:
        value = float(mjd)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return (value - _MJD_UNIX_EPOCH) * 86400.0


def fits_time_to_utc(header: dict) -> Optional[float]:
    """按 FITS 惯例从 header 里找观测起始时刻。MJD 优先于字符串日期。"""
    for key in ("MJD-OBS", "MJDSTART"):
        if header.get(key) is not None:
            t = fits_mjd_to_utc(header[key])
            if t is not None:
                return t
    for key in ("DATE-OBS", "DATE", "TSTART", "STARTTIME", "TIMEOBS"):
        raw = header.get(key)
        if raw is None:
            continue
        if key == "TSTART" and isinstance(raw, (int, float)) and float(raw) < 1e9:
            # TSTART 多为相对秒，不能当绝对时间用
            continue
        t = to_utc_seconds(raw)
        if t is not None:
            return t
    return None


def _flatten_header(header) -> dict:
    out: dict = {}
    for key, value in header.items():
        if key is None or key in out:
            continue
        out[key] = value if isinstance(value, (str, int, float, bool)) or value is None else str(value)
    return out


def _strip_quotes(value) -> Optional[str]:
    if value is None:
        return None
    return str(value).strip().strip("'\" ")


def _palette_of(img) -> Optional[list]:
    """调色板模式（分割掩膜、标签图）的配色就是数据的语义，必须随元数据带出去。

    PIL 的 getpalette() 总是补零到 768 项，这里裁掉尾部纯黑，
    否则每个掩膜项的 meta 里都会挂上 700 多个 0。
    """
    if img.mode != "P":
        return None
    raw = img.getpalette() or []
    triples = [tuple(raw[i:i + 3]) for i in range(0, len(raw) - 2, 3)]
    while triples and triples[-1] == (0, 0, 0):
        triples.pop()
    return [list(c) for c in triples] or None


def load_image(uri: str, title: Optional[str] = None) -> DataItem:
    """PNG/JPEG/BMP 等常规图像。EXIF 时间作为 t_ref。"""
    path = Path(uri)
    with Image.open(path) as img:
        shape = (img.height, img.width)
        mode = img.mode
        fmt = img.format or path.suffix.lstrip(".")
        info = dict(img.info or {})
        exif_time = info.get("DateTimeOriginal") or info.get("DateTime")
        palette = _palette_of(img)

    def opener():
        with Image.open(path) as im:
            # 这里刻意返回调色板的"索引"而不是换算后的 RGB：
            # 分析算子要的是类别编号，配色交给渲染层
            return np.asarray(im)

    return DataItem(
        id=path.name,
        modality=_modality_for(len(shape)),
        uri=str(path),
        title=title or path.name,
        t_ref=to_utc_seconds(exif_time),
        meta={"format": fmt, "mode": mode, "original_dtype": mode, "palette": palette},
        shape=shape,
        dtype=mode,
        n_views=1,
        _opener=opener,
    )


def load_scientific_tiff(uri: str, title: Optional[str] = None) -> DataItem:
    """16/32 位科学 TIFF。多页 TIFF 视为切片序列。"""
    import imageio

    path = Path(uri)
    reader = imageio.get_reader(str(path))
    try:
        n_pages = len(reader)
        first = reader.get_data(0)
        shape, dtype = first.shape, str(first.dtype)
    finally:
        reader.close()

    if n_pages == 1:
        def opener():
            r = imageio.get_reader(str(path))
            try:
                return r.get_data(0)
            finally:
                r.close()
    else:
        def opener():
            r = imageio.get_reader(str(path))
            try:
                return np.stack([r.get_data(i) for i in range(len(r))])
            finally:
                r.close()

    return DataItem(
        id=path.name,
        modality=_modality_for(len(shape)),
        uri=str(path),
        title=title or path.name,
        meta={"format": "TIFF", "n_pages": n_pages},
        shape=shape,
        dtype=dtype,
        n_views=_n_views_for((n_pages,) + tuple(shape)) if n_pages > 1 else 1,
        _opener=opener,
    )


def load_fits(uri: str, hdul_index: Optional[int] = None, title: Optional[str] = None) -> DataItem:
    """FITS。默认取第一个有 2 维以上数据的 HDU。"""
    try:
        from astropy.io import fits
    except ImportError as exc:
        raise ImportError("读取 FITS 需要 astropy，请先安装：pip install astropy") from exc

    path = Path(uri)
    with fits.open(path, memmap=False) as hdul:
        idx = hdul_index if hdul_index is not None else _first_image_hdu_index(hdul)
        hdu = hdul[idx]
        header = _flatten_header(hdu.header)
        data = hdu.data
        if data is None:
            raise ValueError(f"{uri} 的 HDU[{idx}] 没有数据")
        arr = np.asarray(data)
        shape, dtype = arr.shape, str(arr.dtype)

    unit = _strip_quotes(header.get("BUNIT"))
    t_ref = fits_time_to_utc(header)
    t_end = (
        to_utc_seconds(header.get("DATE-END"))
        or fits_mjd_to_utc(header.get("MJD-END"))
    )
    if t_end is None and t_ref is not None:
        exposure = header.get("EXPTIME")
        if isinstance(exposure, (int, float)) and math.isfinite(float(exposure)):
            t_end = t_ref + float(exposure)

    meta = {
        "format": "FITS",
        "hdu": idx,
        "hdu_name": str(header.get("__name__", "")),
        "unit": unit,
        "header": header,
        "original_dtype": dtype,
        "telescope": _strip_quotes(header.get("TELESCOP")),
        "instrument": _strip_quotes(header.get("INSTRUME")),
        "object": _strip_quotes(header.get("OBJECT")),
        "detector": _strip_quotes(header.get("DETECTOR")),
    }

    def opener():
        with fits.open(path, memmap=False) as h:
            return np.asarray(h[idx].data)

    return DataItem(
        id=path.name,
        modality=_modality_for(len(shape)),
        uri=str(path),
        title=title or _strip_quotes(header.get("OBJECT")) or path.name,
        t_ref=t_ref,
        t_end=t_end,
        meta=meta,
        shape=shape,
        dtype=dtype,
        n_views=_n_views_for(shape),
        _opener=opener,
    )


def _first_image_hdu_index(hdul) -> int:
    for i, hdu in enumerate(hdul):
        data = getattr(hdu, "data", None)
        if data is not None and np.ndim(data) >= 2:
            return i
    return 0


def slice_at(item: DataItem, index: int) -> np.ndarray:
    """立方体取单切片；2 维图像原样返回。"""
    arr = item.load()
    if arr.ndim <= 2:
        return arr
    return arr[int(np.clip(index, 0, arr.shape[0] - 1))]


def load(uri: str, **kwargs) -> DataItem:
    suffix = Path(uri).suffix.lower()
    if suffix in FITS_SUFFIXES:
        return load_fits(uri, **kwargs)
    if suffix in SCIENTIFIC_IMAGE_SUFFIXES:
        return load_scientific_tiff(uri, **kwargs)
    if suffix in IMAGE_SUFFIXES:
        return load_image(uri, **kwargs)
    raise ValueError(f"不支持的图像扩展名: {suffix or '(无)'} ({uri})")


def register(registry) -> None:
    registry.add(
        "fits", load_fits, "FITS 图像与数据立方体",
        {"hdul_index": {"type": "integer", "default": None, "description": "指定 HDU 序号，缺省自动选首个有数据的 HDU"}},
    )
    registry.add("tiff", load_scientific_tiff, "16/32 位科学 TIFF，多页作切片序列")
    registry.add("image", load_image, "PNG/JPEG/BMP 常规图像")
