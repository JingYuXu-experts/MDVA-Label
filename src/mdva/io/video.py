"""视频 / 帧序列模态。

三种后端按可用性与代价排序，共同实现 FrameSource 协议：
- 目录帧序列：零额外依赖，且科学流水线里大量数据本来就是解帧后的图像序列
- npy 帧栈：内存映射，(t,y,x) 大栈不会整份进内存
- 视频文件：走 imageio+ffmpeg，随机取帧代价与编码关键帧间隔相关，
  因此内部维护游标与近邻缓存，避免每取一帧就重开解码器

视频的时间基准 t_ref 通常拿不到（容器里没有绝对时刻），所以支持显式
传入或同名 .meta.json 侧车文件；没有 t_ref 时视频只参与切片浏览，
不参与跨模态联动——宁可少联动，也不要错联动。
"""

from __future__ import annotations

import json
import math
import re
from collections import OrderedDict
from pathlib import Path
from typing import Optional

import numpy as np

from ..core.model import DataItem, Modality

VIDEO_SUFFIXES = (".mp4", ".avi", ".mov", ".mkv", ".webm", ".gif", ".m4v", ".mpg", ".mpeg")
STACK_SUFFIXES = (".npy",)
FRAME_DIR_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)s_frame_(\d+)", re.IGNORECASE)


class FrameSource:
    """帧序列的统一接口。子类必须给出 n_frames/fps/size 与 frame(i)。"""

    kind = "frames"
    sidecar: dict = {}

    def __init__(self):
        self._cache: "OrderedDict[int, np.ndarray]" = OrderedDict()
        self.cache_size = 8

    @property
    def n_frames(self) -> int:
        raise NotImplementedError

    @property
    def fps(self) -> float:
        return 1.0

    @property
    def size(self) -> tuple:
        """(height, width)"""
        raise NotImplementedError

    @property
    def t0(self) -> Optional[float]:
        return None

    def _decode(self, index: int) -> np.ndarray:
        raise NotImplementedError

    def frame(self, index: int) -> np.ndarray:
        i = int(np.clip(index, 0, max(0, self.n_frames - 1)))
        if i in self._cache:
            self._cache.move_to_end(i)
            return self._cache[i]
        arr = self._decode(i)
        self._cache[i] = arr
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return arr

    def time_of(self, index: int) -> Optional[float]:
        if self.t0 is None or self.fps <= 0:
            return None
        return self.t0 + index / self.fps

    def index_of(self, t: float) -> int:
        if self.t0 is None or self.fps <= 0:
            return 0
        return int(np.clip(round((t - self.t0) * self.fps), 0, max(0, self.n_frames - 1)))

    @property
    def duration(self) -> float:
        if self.fps <= 0:
            return 0.0
        return max(0, self.n_frames - 1) / self.fps

    def close(self) -> None:
        self._cache.clear()

    def meta(self) -> dict:
        h, w = self.size
        return {
            "backend": self.kind,
            "n_frames": self.n_frames,
            "fps": round(float(self.fps), 6),
            "duration": round(self.duration, 4),
            "width": int(w),
            "height": int(h),
        }


class DirectoryFrames(FrameSource):
    """一个目录里的编号图像即为帧序列。"""

    kind = "frame_dir"

    def __init__(self, directory, fps: float = 1.0, t0: Optional[float] = None):
        super().__init__()
        from .image import IMAGE_SUFFIXES, SCIENTIFIC_IMAGE_SUFFIXES

        d = Path(directory)
        exts = set(IMAGE_SUFFIXES) | set(SCIENTIFIC_IMAGE_SUFFIXES)
        self.paths = sorted((p for p in d.iterdir()
                             if p.is_file() and p.suffix.lower() in exts),
                            key=_natural_key)
        if not self.paths:
            raise ValueError(f"{directory} 下没有可识别的帧图像")
        self._fps = float(fps) if fps and fps > 0 else 1.0
        self._t0 = t0 if t0 is not None else _t0_from_names(self.paths, self._fps)
        self._size = None

    @property
    def n_frames(self) -> int:
        return len(self.paths)

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def t0(self) -> Optional[float]:
        return self._t0

    @property
    def size(self) -> tuple:
        if self._size is None:
            from PIL import Image

            with Image.open(self.paths[0]) as img:
                self._size = (img.height, img.width)
        return self._size

    def _decode(self, index: int) -> np.ndarray:
        from .image import load

        return load(str(self.paths[index])).load()


class NpyStackFrames(FrameSource):
    """.npy 里的 (t, y, x) 或 (t, y, x, c) 帧栈，内存映射读取。"""

    kind = "npy_stack"

    def __init__(self, uri, fps: float = 1.0, t0: Optional[float] = None):
        super().__init__()
        self.path = Path(uri)
        self.stack = np.load(self.path, mmap_mode="r")
        if self.stack.ndim < 3:
            raise ValueError(f"{uri} 不是帧栈（需要 (t,y,x) 及以上），实际 {self.stack.shape}")
        self._fps = float(fps) if fps and fps > 0 else 1.0
        self._t0 = t0

    @property
    def n_frames(self) -> int:
        return int(self.stack.shape[0])

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def t0(self) -> Optional[float]:
        return self._t0

    @property
    def size(self) -> tuple:
        return (int(self.stack.shape[1]), int(self.stack.shape[2]))

    def _decode(self, index: int) -> np.ndarray:
        return np.asarray(self.stack[index])

    def close(self) -> None:
        self.stack = None
        super().close()


class VideoFileFrames(FrameSource):
    """容器视频。reader 常驻，顺序前进靠 next，跳帧才重新 seek。"""

    kind = "video_file"

    def __init__(self, uri, fps: Optional[float] = None, t0: Optional[float] = None):
        super().__init__()
        try:
            import imageio
        except ImportError as exc:
            raise ImportError("读取视频文件需要 imageio（及 imageio-ffmpeg）") from exc

        self.path = Path(uri)
        self._reader = imageio.get_reader(str(self.path))
        meta = self._probe()
        self._fps = float(fps) if fps and fps > 0 else meta["fps"]
        self._n = meta["n_frames"]
        self._size_hw = meta["size"]
        self._t0 = t0 if t0 is not None else meta.get("t0")
        self._cursor = -1
        self._iterator = None

    def _probe(self) -> dict:
        out = {"fps": 25.0, "n_frames": 0, "size": (0, 0), "t0": None}
        try:
            first = self._reader.get_data(0)
            out["size"] = (int(first.shape[0]), int(first.shape[1]))
        except Exception:
            pass
        try:
            md = dict(self._reader.get_meta_data())
        except Exception:
            md = {}
        fps = md.get("fps")
        has_fps = bool(fps) and float(fps) > 0
        if has_fps:
            out["fps"] = float(fps)
        out["n_frames"] = self._exact_count() or self._count_from_meta(md, out["fps"], has_fps)
        if not out["n_frames"]:
            out["n_frames"] = 1
        return out

    def _exact_count(self) -> int:
        """能拿到精确帧数就绝不估算。

        GIF 之类的调色板动图 duration 是"每帧毫秒"，而 ffmpeg 的是"整段秒"，
        用 duration×fps 统一推算会差一个数量级，而且要到取越界帧时才暴露。
        """
        counter = getattr(self._reader, "count_frames", None)
        if counter is not None:
            try:
                return int(counter())
            except Exception:
                pass
        try:
            n = len(self._reader)
        except Exception:
            return 0
        if isinstance(n, float) and not math.isfinite(n):
            return 0
        try:
            n = int(n)
        except (TypeError, ValueError):
            return 0
        return n if 0 < n < 10 ** 7 else 0

    @staticmethod
    def _count_from_meta(md, fps: float, has_fps: bool) -> int:
        # 只有 ffmpeg 这类同时报告 fps 的后端，duration 才是"整段秒数"
        if not has_fps:
            return 0
        try:
            duration = float(md.get("duration"))
        except (TypeError, ValueError):
            return 0
        return int(round(duration * fps)) if duration > 0 else 0

    @property
    def n_frames(self) -> int:
        return self._n

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def t0(self) -> Optional[float]:
        return self._t0

    @property
    def size(self) -> tuple:
        return self._size_hw

    def _decode(self, index: int) -> np.ndarray:
        if index == self._cursor + 1 and self._iterator is not None:
            try:
                frame = next(self._iterator)
                self._cursor = index
                return np.asarray(frame)
            except StopIteration:
                self._iterator = None
        # 跳帧：重新定位。ffmpeg 后端按关键帧回退搜索，跨度越大越慢，
        # 所以定位后立刻切回顺序迭代，拖动进度条才是线性代价。
        self._iterator = None
        frame = np.asarray(self._reader.get_data(index))
        self._cursor = index
        iter_data = getattr(self._reader, "iter_data", None)
        if iter_data is not None and index + 1 < self._n:
            try:
                self._iterator = iter_data(start=index + 1)
            except Exception:
                self._iterator = None
        return frame

    def close(self) -> None:
        self._iterator = None
        try:
            self._reader.close()
        except Exception:
            pass
        super().close()


def _natural_key(path: Path) -> tuple:
    """数字部分按数值排，避免 frame_10 排在 frame_2 前面。"""
    parts = re.split(r"(\d+)", path.name)
    return tuple((0, int(p)) if p.isdigit() else (1, p.lower()) for p in parts)


def _t0_from_names(paths, fps: float) -> Optional[float]:
    """从 `12.5s_frame_3.jpeg` 这类名字里取最小相对时刻。

    这是相对秒而非绝对 UTC，所以只作为 t0=0 的相对基准，不做联动。
    """
    stamps = []
    for p in paths[:50]:
        m = FRAME_DIR_PATTERN.match(p.name)
        if m:
            stamps.append(float(m.group(1)) + (int(m.group(2)) - 1) / max(fps, 1e-6))
    return min(stamps) if stamps else None


def sidecar_for(uri) -> Optional[Path]:
    """帧序列的旁路元文件位置。目录用 meta.json，文件用同名 .meta.json。"""
    path = Path(uri)
    if path.is_dir():
        for cand in (path / "meta.json", path / f"{path.name}.meta.json"):
            if cand.is_file():
                return cand
        return None
    for cand in (path.with_suffix(".meta.json"), Path(str(path) + ".meta.json")):
        if cand.is_file():
            return cand
    return None


def _read_sidecar(uri) -> dict:
    """旁路元文件提供绝对起始时刻与帧率——没有它们视频无法与遥测联动。"""
    path = sidecar_for(uri)
    if path is None:
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}


def is_frame_dir(uri) -> bool:
    """目录只有自带 meta.json 时才当作帧序列。

    否则任何图像目录都会被折叠成一项，而"一目录多张图分别浏览"正是这个
    工具的本职，不能靠启发式猜测把它抢走。
    """
    path = Path(uri)
    return path.is_dir() and sidecar_for(path) is not None


def make_source(uri, fps: Optional[float] = None, t0: Optional[float] = None) -> FrameSource:
    path = Path(uri)
    side = _read_sidecar(path)
    fps = fps or side.get("fps")
    t0 = t0 if t0 is not None else side.get("t0")

    if path.is_dir():
        src = DirectoryFrames(path, fps=fps or 1.0, t0=t0)
    elif path.suffix.lower() in STACK_SUFFIXES:
        src = NpyStackFrames(path, fps=fps or 1.0, t0=t0)
    elif path.suffix.lower() in VIDEO_SUFFIXES or path.is_file():
        src = VideoFileFrames(path, fps=fps, t0=t0)
    else:
        raise ValueError(f"无法作为帧序列打开: {uri}")
    src.sidecar = side
    return src


def load_video(uri, fps: Optional[float] = None, t0: Optional[float] = None,
               title: Optional[str] = None) -> DataItem:
    source = make_source(uri, fps=fps, t0=t0)
    meta = source.meta()
    meta["t0"] = source.t0
    for key, value in (source.sidecar or {}).items():
        if key not in ("fps", "t0"):
            meta.setdefault(key, value)
    shape = (meta["n_frames"], meta["height"], meta["width"])

    def opener():
        # 帧栈整体物化；目录与视频只给逐帧访问器，避免一次性吃满内存
        if source.kind == "npy_stack":
            return np.asarray(source.stack)
        return source

    return DataItem(
        id=Path(uri).name,
        modality=Modality.VIDEO.value,
        uri=str(uri),
        title=title or Path(uri).name,
        t_ref=source.t0,
        t_end=source.time_of(max(0, meta["n_frames"] - 1)),
        meta=meta,
        shape=shape,
        dtype="frame",
        n_views=meta["n_frames"],
        _opener=opener,
    )


def frame_at(item: DataItem, index: int) -> np.ndarray:
    src = item.load()
    if isinstance(src, FrameSource):
        return src.frame(index)
    return np.asarray(src)[int(np.clip(index, 0, src.shape[0] - 1))]


def frame_time(item: DataItem, index: int) -> Optional[float]:
    src = item.load()
    if isinstance(src, FrameSource):
        return src.time_of(index)
    if item.t_ref is None or not item.meta.get("fps"):
        return None
    return item.t_ref + index / float(item.meta["fps"])


def time_to_frame(item: DataItem, t: float) -> int:
    src = item.load()
    if isinstance(src, FrameSource):
        return src.index_of(t)
    fps = float(item.meta.get("fps") or 0)
    if item.t_ref is None or fps <= 0:
        return 0
    return int(np.clip(round((t - item.t_ref) * fps), 0, max(0, item.n_views - 1)))


def to_display_uint8(arr: np.ndarray) -> np.ndarray:
    """导出用的线性 8 位映射，不做显示拉伸。

    刻意不依赖 render 层：帧导出往往是要喂给外部工具的中间产物，
    需要保持数值单调而非视觉最优；视觉拉伸请走 render 管线。
    """
    a = np.asarray(arr, dtype=np.float64)
    finite = a[np.isfinite(a)]
    if finite.size == 0:
        return np.zeros(a.shape, dtype=np.uint8)
    lo, hi = float(finite.min()), float(finite.max())
    if hi <= lo:
        return np.zeros(a.shape, dtype=np.uint8)
    # 坏像元画成背景而不是留 NaN：uint8 无法表示 NaN，直接转型会回绕成随机值
    scaled = np.clip((a - lo) * (255.0 / (hi - lo)), 0.0, 255.0)
    return np.nan_to_num(scaled, nan=0.0, posinf=255.0, neginf=0.0).astype(np.uint8)


def export_frames(item: DataItem, out_dir, start: int = 0, stop: Optional[int] = None,
                  step: int = 1, fmt: str = "png") -> list:
    """把帧区间导出为图像序列，供报告与外部工具使用。"""
    from PIL import Image

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    src = item.load()
    total = item.n_views
    stop = total if stop is None else min(int(stop), total)
    step = max(1, int(step))
    written = []
    for i in range(max(0, int(start)), stop, step):
        arr = src.frame(i) if isinstance(src, FrameSource) else np.asarray(src)[i]
        rgb = to_display_uint8(np.asarray(arr))
        if rgb.ndim == 2:
            rgb = np.stack([rgb] * 3, axis=-1)
        path = out_dir / f"{i:06d}.{fmt}"
        Image.fromarray(rgb).save(str(path))
        written.append(str(path))
    return written


def close(item: DataItem) -> None:
    src = item.load() if item.has_data else None
    if isinstance(src, FrameSource):
        src.close()


def load(uri: str, **kwargs) -> DataItem:
    return load_video(uri, **kwargs)


def register(registry) -> None:
    registry.add(
        "video", load_video, "帧序列 / 视频 / npy 帧栈",
        {
            "fps": {"type": "number", "default": None, "description": "覆盖探测到的帧率"},
            "t0": {"type": "number", "default": None, "description": "绝对起始 UTC 秒，缺省则不参与跨模态联动"},
        },
    )
