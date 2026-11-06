"""L1 数据接入层：按模态分派的多模态加载入口。"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from ..core.model import DataItem, Modality, Slice
from . import event, image, series, video
from .timeutil import from_utc_seconds, to_utc_seconds
from .series import Calibration

MODALITY_LOADERS = {
    Modality.IMAGE.value: image.load,
    Modality.CUBE.value: image.load,
    Modality.SERIES.value: series.load,
    Modality.EVENT.value: event.load,
    Modality.VIDEO.value: video.load,
}

_SUFFIX_MODALITY = {}
for _sfx in image.FITS_SUFFIXES + image.SCIENTIFIC_IMAGE_SUFFIXES + image.IMAGE_SUFFIXES:
    _SUFFIX_MODALITY[_sfx] = Modality.IMAGE.value
for _sfx in series.SERIES_SUFFIXES:
    _SUFFIX_MODALITY[_sfx] = Modality.SERIES.value
for _sfx in event.EVENT_SUFFIXES:
    _SUFFIX_MODALITY[_sfx] = Modality.EVENT.value
# 视频文件本身是一个数据项（帧在内部随机访问），与"把目录当帧序列"是两回事
for _sfx in video.VIDEO_SUFFIXES:
    _SUFFIX_MODALITY[_sfx] = Modality.VIDEO.value

# .csv/.txt/.json 既能是时序也能是事件，需要按内容嗅探
_AMBIGUOUS = (".csv", ".txt", ".dat", ".json")
# .npy/.npz 既可能是 (n, channels) 时序表，也可能是 (t, y, x) 帧栈，按维数判定
_BY_SHAPE = (".npy", ".npz")

# 加载器的旁路元数据文件，不是数据项本身
_SKIP_NAMES = ("truth.json", "manifest.json", "meta.json", "calib.json", "mdva_manifest.json")
_SKIP_SUBSTRINGS = (".calib.", ".meta.")


def modality_of(uri) -> Optional[str]:
    """接受完整路径或裸扩展名两种写法。

    Path('.fits').suffix 是空串（被当成隐藏文件名），只按后缀调用时必须兜住。
    """
    text = str(uri).lower()
    if text in _SUFFIX_MODALITY:
        return _SUFFIX_MODALITY[text]
    return _SUFFIX_MODALITY.get(Path(text).suffix)


def _looks_like_events(payload) -> bool:
    keys = set()
    if isinstance(payload, dict):
        keys.update(k.lower() for k in payload)
        payload = payload.get("events") or payload.get("rows") or [payload]
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        keys.update(k.lower() for k in payload[0])
    return bool(keys & {"label", "event", "message", "severity", "msg", "text", "desc", "description"}) \
        or "events" in keys


def _stack_ndim(uri) -> Optional[int]:
    """只读数组头部信息判断维数，不把整个 .npy 读进内存。"""
    try:
        import numpy as np

        path = Path(uri)
        if path.suffix.lower() == ".npz":
            with np.load(path) as zf:
                shapes = [zf[k].shape for k in zf.files]
            return max((len(s) for s in shapes), default=None)
        with open(path, "rb") as fh:
            version = np.lib.format.read_magic(fh)
            shape, _fortran, _dtype = np.lib.format._read_array_header(fh, version)
        return len(shape)
    except Exception:
        return None


def _fits_ndim(uri) -> Optional[int]:
    """主头 NAXIS——2880 字节块 × 80 字符卡，不必为嗅探一个数字开整个 astropy。

    只读主 HDU；主头为空的扩展型 FITS 读不到维数，按 image 口径回退，
    与改动前行为一致，不做无界的整文件扫描。
    """
    try:
        with open(uri, "rb") as fh:
            block = fh.read(2880)
    except OSError:
        return None
    for i in range(0, len(block) - 79, 80):
        card = block[i:i + 80].decode("ascii", "replace")
        if card.split("/")[0].strip() == "END":
            return None
        key, sep, value = card.partition("=")
        if sep and key.strip() == "NAXIS":
            try:
                return int(value.split("/")[0].strip())
            except ValueError:
                return None
    return None


def _is_number(cell: str) -> bool:
    try:
        float(cell)
    except (TypeError, ValueError):
        return False
    return True


def sniff(uri) -> str:
    """歧义扩展名按内容判定，而不是只看文件首行——缩进 JSON 的首行只是 "{"。

    返回 "" 表示"这不是一个数据项"。宁可跳过，也不要给一张指标表编出一条
    不存在的时间轴——那样产生的联动和窗口都是假的。
    """
    path = Path(uri)
    if path.is_dir():
        return Modality.VIDEO.value if video.is_frame_dir(path) else ""
    suffix = path.suffix.lower()

    if suffix in _BY_SHAPE:
        ndim = _stack_ndim(uri)
        if ndim is not None and ndim >= 3:
            return Modality.VIDEO.value
        return Modality.SERIES.value

    if suffix in image.FITS_SUFFIXES:
        # FITS 按表观后缀只能是 image，但 3 维数据加载后是 cube。
        # items/scan 用 sniff、frame/meta 用加载结果，两处必须同一口径，
        # 否则前端会看到同一个文件两个模态。解析主头 NAXIS，读不到就退回后缀表。
        ndim = _fits_ndim(uri)
        if ndim is not None and ndim >= 3:
            return Modality.CUBE.value
        return modality_of(suffix) or ""

    if suffix not in _AMBIGUOUS:
        return modality_of(suffix) or ""

    if suffix == ".json":
        import json

        try:
            return Modality.EVENT.value if _looks_like_events(json.loads(path.read_text(encoding="utf-8-sig"))) \
                else Modality.SERIES.value
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return Modality.SERIES.value

    lines = [ln for ln in path.read_text(encoding="utf-8-sig", errors="replace")[:4096].splitlines() if ln.strip()]
    if not lines:
        return Modality.SERIES.value
    rows = [[c.strip() for c in ln.replace("\t", ",").split(",")] for ln in lines[:2]]
    if series._find_time_column(rows[0]) is not None:
        return Modality.SERIES.value
    # 没有时间列时，只有纯数值表才配得上"按采样序号补时间"这条兜底路径
    body = rows[1] if len(rows) > 1 else rows[0]
    return Modality.SERIES.value if all(_is_number(c) for c in body) else ""


def _is_sidecar(path: Path) -> bool:
    name = path.name.lower()
    return name in _SKIP_NAMES or any(s in name for s in _SKIP_SUBSTRINGS)


def _auto_calibration(path: Path) -> Optional[Path]:
    """时序文件旁边的标定侧车：优先同名 <stem>.calib.json，其次同目录 calib.json。

    离散通道的枚举语义只能从标定表得到，缺了表就会把"工作模式"当连续量
    去插值和求均值，产出一堆不存在的中间状态。因此宁可自动去找。
    """
    same = path.with_name(path.stem + ".calib.json")
    if same.is_file():
        return same
    generic = path.parent / "calib.json"
    return generic if generic.is_file() else None


def load_any(uri: str, modality: Optional[str] = None, **kwargs) -> DataItem:
    path = Path(uri)
    kind = modality or sniff(uri)
    if not kind and path.is_dir():
        # 自动扫描只折叠"自带 meta.json"的目录，但用户显式点名一个目录时意图已经
        # 很清楚：按帧序列打开。文件挑选界面必须能直接播。
        kind = Modality.VIDEO.value
    loader = MODALITY_LOADERS.get(kind) if kind else None
    if loader is None:
        raise ValueError(f"未知模态: {kind or uri}")
    if kind == Modality.SERIES.value:
        calib = kwargs.get("calibration")
        if calib is None:
            calib = _auto_calibration(path)
        if isinstance(calib, (str, Path)):
            kwargs["calibration"] = Calibration.load(calib)
    return loader(uri, **kwargs)


def _walk(root: Path, recursive: bool):
    """展开候选项。被声明为帧序列的目录整体作为一项并剪枝，不再逐文件展开。"""
    for path in sorted(root.iterdir(), key=lambda p: str(p).lower()):
        if path.is_dir():
            if video.is_frame_dir(path):
                yield path
            elif recursive:
                yield from _walk(path, True)
        else:
            yield path


def scan(root, recursive: bool = True, limit: Optional[int] = None, **load_kwargs) -> list:
    """把一个目录/文件列表展开为 DataItem 集合。只探测头，不读数据。"""
    root = Path(root)
    if root.is_file() or video.is_frame_dir(root):
        candidates = [root]
    else:
        candidates = list(_walk(root, recursive))

    items = []
    for path in candidates:
        if path.is_file() and _is_sidecar(path):
            continue
        kind = sniff(str(path))
        if not kind:
            continue
        try:
            items.append(load_any(str(path), modality=kind, **load_kwargs))
        except Exception as exc:
            print(f"[warn] 跳过 {path.name}: {exc}")
        if limit and len(items) >= limit:
            break
    return items


def common_span(items: Iterable[DataItem]) -> tuple:
    """只统计有绝对时间基准的项。

    没有 t_ref 的项其 time_span 是全域，混进来会把整条时间轴撑成无限宽，
    一个 CT 断层就能让二十分钟的遥测在界面上缩成一个点。
    """
    t0, t1 = float("inf"), float("-inf")
    for item in items:
        if item.t_ref is None:
            continue
        a, b = item.time_span
        t0 = min(t0, a)
        t1 = max(t1, b)
    if t0 == float("inf"):
        return (float("-inf"), float("inf"))
    return (t0, t1)


def items_in_window(items: list, sl: Slice) -> list:
    return [i for i in items if i.overlaps(sl)]


__all__ = [
    "Calibration",
    "MODALITY_LOADERS",
    "common_span",
    "event",
    "from_utc_seconds",
    "image",
    "items_in_window",
    "load_any",
    "modality_of",
    "scan",
    "series",
    "sniff",
    "to_utc_seconds",
    "video",
]
