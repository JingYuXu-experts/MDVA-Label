"""L6 服务门面：把引擎能力整理成"一次调用 = 一个界面动作"的函数。

FastAPI / 标准库 HTTP / 桌面壳都只是这层的适配器，所以：
- 不装 fastapi 也能把这层完整测一遍（`test_services.py`）；
- 界面拿到的永远是 JSON 安全的 dict，字段名一次定死，前端不用猜；
- 所有渲染结果都带 `(uri, 窗口, 尺寸, 样式)` 缓存——时间轴拖动时同一帧会被反复要，
  不缓存的话首帧 40ms 的开销会直接变成肉眼可见的卡顿。

这一层不发起任何出站请求，也不写原始数据文件：写盘只发生在明确点"执行"的
清洗/增强/导出/收录动作里，而且一律写到数据集之外的输出目录。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

from ..annotate import state as ST
from ..annotate.model import Ann
from ..annotate.store import DATASET_TYPES, Store
from ..core.model import Slice
from .. import io as MDIO
from .. import render as RD

DEFAULT_CELL = (196, 122)
FRAME_CACHE = 96
ITEM_CACHE = 24
SCAN_TTL = 30.0
MAX_OVERLAY = 200
JSON_SAFE_PRIMITIVES = (str, int, float, bool, type(None))


def _plain(value) -> Any:
    """递归转成 JSON 安全的值：numpy 标量、ndarray、dataclass 都可能从下层冒出来。"""
    if isinstance(value, JSON_SAFE_PRIMITIVES):
        return value
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        f = float(value)
        return f if f == f and abs(f) != float("inf") else None
    if isinstance(value, np.ndarray):
        return [_plain(v) for v in value.ravel().tolist()]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return str(value)


def png_b64(rgb: np.ndarray) -> str:
    """RGB 数组 -> base64 PNG（不带 data: 前缀，前端自己拼）。"""
    from PIL import Image

    import io as _io

    buf = _io.BytesIO()
    Image.fromarray(np.ascontiguousarray(np.asarray(rgb, dtype=np.uint8))).save(
        buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def slice_of(params: dict) -> Slice:
    """查询参数里的 index / t0 / t1 -> Slice（两种语义在同一层里共存）。"""
    index = int(params.get("index") or 0)
    t0 = params.get("t0")
    t1 = params.get("t1")
    if t0 is not None or t1 is not None:
        return Slice(index=index, t0=float(t0 if t0 is not None else -np.inf),
                     t1=float(t1 if t1 is not None else np.inf))
    return Slice(index=index)


def style_of(params: dict) -> dict:
    """界面渲染样式：只有显式给出的键才进 style，其余走 L2 的默认（zscale + linear）。"""
    # 键名必须与 L2 的 render_array / render_channel 形参一致：多一个键就是
    # TypeError，而界面每个滑块都会经这里，拼错一次整页图都出不来。
    numeric = ("gamma", "vmin", "vmax", "ymin", "ymax", "out_width")
    text = ("interval", "stretch", "colormap", "method")
    style = {}
    for key in text:
        if params.get(key) not in (None, ""):
            style[key] = str(params[key])
    for key in numeric:
        if params.get(key) not in (None, ""):
            style[key] = float(params[key])
    for key in ("invert",):
        if params.get(key) not in (None, ""):
            style[key] = str(params[key]).lower() in ("1", "true", "yes")
    for key in ("channel",):
        if params.get(key) not in (None, ""):
            style[key] = int(params[key])
    if params.get("palette"):
        style["palette"] = params["palette"]
    return style


def probe_raw():
    """能力探测（CPU/内存/显卡档位）。后端可用性判断和推荐打分都要用它。"""
    from ..backends.probe import detect

    return detect()


#: sniff 需要读内容才能定夺的扩展名（与 io._AMBIGUOUS/_BY_SHAPE 口径一致）。
#: 文件还在磁盘上时永远走 sniff；这份表只服务"已标注但文件没了"的回退。
_AMBIGUOUS_SUFFIXES = (".csv", ".txt", ".dat", ".json", ".tsv", ".npy", ".npz")


def _safe_sniff(uri) -> str:
    """sniff 对歧义扩展名会读文件内容；已标注但磁盘上没了的 uri 不能让整页列表崩掉。

    回退只认无歧义扩展名（.png/.fits/...）；歧义扩展名没了内容就无从判断，
    如实返回 ""（unknown），也不让错误猜测污染 modality 过滤。
    """
    try:
        return MDIO.sniff(uri)
    except OSError:
        suffix = Path(uri).suffix.lower()
        if suffix in _AMBIGUOUS_SUFFIXES:
            return ""
        return MDIO.modality_of(suffix) or ""


def _overlay_anns(overlay, backend: str = "", index: Optional[int] = None) -> list:
    """叠加层 -> Ann 列表。

    顺序固定为 boxes/points/polylines/intervals：网页画布按这个顺序叠色，
    后端预览图（draw_overlay）也按这个顺序取色，两边颜色才对得上。
    """
    out = []
    for group in (overlay.boxes, overlay.points, overlay.polylines, overlay.intervals):
        for obj in group:
            if len(out) >= MAX_OVERLAY:
                return out
            out.append(Ann.from_overlay(obj, source="preannotate", backend=backend,
                                        slice=index))
    return out


class Hub:
    """一个工作区（一个 mdva.sqlite）对应一个 Hub。线程安全靠 Store 的写锁 + 只读渲染。"""

    def __init__(self, db_path, out_dir=None):
        self.path = Path(db_path)
        self.store = Store(self.path)
        self.out_dir = Path(out_dir) if out_dir else self.path.parent / "out"
        self._frames: dict = {}
        self._frame_order: list = []
        # 服务端是多线程的：两个线程同时淘汰会让淘汰队列和字典对不上，缓存上限形同虚设
        self._lock = threading.Lock()
        # 解析结果缓存：遥测 CSV 逐值解析时间戳要 5.7s（渲染本身只要 15ms），
        # 同一文件不换磁盘状态就不该重解析。键含 (size, mtime_ns)，文件一变自动失效。
        self._loaded: dict = {}
        self._loaded_order: list = []
        # 目录扫描缓存：items()/stats() 每次都要磁盘全集，扫描里最贵的还是时序解析
        self._scans: dict = {}

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------------ 加载与扫描缓存

    @staticmethod
    def _fingerprint(path: Path):
        """(size, mtime_ns) 作为"文件没变"的证据；拿不到就返回 None（不缓存）。"""
        try:
            st = path.stat()
            return (st.st_size, st.st_mtime_ns)
        except OSError:
            return None

    def load_item(self, uri, modality: Optional[str] = None):
        """带 LRU 的 MDIO.load_any：同 uri 且磁盘指纹未变时直接复用解析结果。

        帧渲染缓存（_frames）只覆盖"参数完全相同"的重复请求；换通道、换窗口
        会穿透帧缓存重新走 load_any——对遥测 CSV 来说每穿透一次就是 5.7 秒的
        逐值 strptime。这一层把解析结果按文件指纹留住，穿透帧缓存也不再重解析。
        """
        path = Path(str(uri))
        fp = self._fingerprint(path)
        if fp is None:
            # 路径不存在等异常不进缓存，让 load_any 抛出原本的错误信息
            return MDIO.load_any(str(uri), modality=modality or None)
        key = (str(path), modality or "", fp)
        with self._lock:
            cached = self._loaded.get(key)
            if cached is not None:
                self._loaded_order.remove(key)
                self._loaded_order.append(key)
                return cached
        item = MDIO.load_any(str(uri), modality=modality or None)
        with self._lock:
            self._loaded[key] = item
            self._loaded_order.append(key)
            while len(self._loaded_order) > ITEM_CACHE:
                self._loaded.pop(self._loaded_order.pop(0), None)
        return item

    def _scan_items(self, root, recursive: bool = True, limit: Optional[int] = None) -> list:
        """带 TTL + 目录指纹的 MDIO.scan。

        指纹只盯 root 目录本身的 mtime：深层文件增删不一定动 root，所以再叠一层
        短 TTL 兜底；清洗/收录这类明确会动磁盘的动作走 _invalidate_scans() 立即失效。
        """
        root = Path(root)
        key = (str(root), bool(recursive), limit)
        fp = self._fingerprint(root)
        now = time.monotonic()
        with self._lock:
            entry = self._scans.get(key)
            if entry is not None and entry[0] == fp and (now - entry[1]) < SCAN_TTL:
                return entry[2]
        items = MDIO.scan(root, recursive=recursive, limit=limit)
        with self._lock:
            self._scans[key] = (fp, now, items)
        return items

    def _invalidate_scans(self) -> None:
        with self._lock:
            self._scans.clear()

    # ------------------------------------------------------------------ 数据集

    def datasets(self) -> list:
        out = []
        for ds in self.store.list_datasets():
            versions = self.store.list_versions(ds["id"])
            head = versions[-1]["id"] if versions else ""
            out.append({
                "id": ds["id"], "name": ds["name"], "type": ds["type"], "root": ds["root"],
                "meta": ds.get("meta") or {}, "created": ds.get("created"),
                "n_versions": len(versions), "head": head,
                "n_annotations": int(ds.get("n_annotations") or 0),
                "head_annotations": versions[-1]["n_annotations"] if versions else 0,
            })
        return out

    def create_dataset(self, name: str, type: str, root, category: str = "",
                       labels: Sequence = ()) -> dict:
        if type not in DATASET_TYPES:
            raise ValueError(f"未知数据集类型 {type!r}，可用 {list(DATASET_TYPES)}")
        root = Path(root)
        if not root.is_dir():
            raise ValueError(f"数据目录不存在：{root}（先用 mdva datasets 生成，或指向已有目录）")
        did = self.store.create_dataset(name, type, root,
                                        meta={"category": category} if category else {})
        vid = self.store.put_version(did, None, "init")
        payload = [{"name": l} if isinstance(l, str) else dict(l) for l in labels or []]
        if payload:
            self.store.set_labels(did, payload)
        return {"dataset_id": did, "version_id": vid}

    def dataset(self, dataset_id: str) -> dict:
        ds = self.store.get_dataset(dataset_id)
        if ds is None:
            raise KeyError(f"未知数据集 {dataset_id}")
        ds["versions"] = self.versions(dataset_id)
        ds["labels"] = self.store.list_labels(dataset_id)
        return ds

    def set_labels(self, dataset_id: str, labels: Sequence) -> list:
        """整体替换标签组（顺序即界面顺序），返回替换后的标签。"""
        payload = [{"name": l} if isinstance(l, str) else dict(l) for l in labels or []]
        self.store.set_labels(dataset_id, payload)
        return self.store.list_labels(dataset_id)

    def versions(self, dataset_id: str) -> list:
        out = []
        for v in self.store.list_versions(dataset_id):
            states = {}
            for row in self.store.list_annotations(v["id"]):
                states[row["state"]] = states.get(row["state"], 0) + 1
            out.append({**v, "states": states})
        return out

    def head_version(self, dataset_id: str, version_id: str = "") -> str:
        if version_id:
            if self.store.get_version(version_id) is None:
                raise KeyError(f"未知版本 {version_id}")
            return version_id
        versions = self.store.list_versions(dataset_id)
        if not versions:
            raise KeyError(f"数据集 {dataset_id} 还没有版本")
        return versions[-1]["id"]

    def manifest(self, dataset_id: str) -> dict:
        return self.store.export_manifest(dataset_id, write=True)

    def scan(self, root, recursive: bool = True, limit: int = 2000) -> dict:
        t0 = time.perf_counter()
        items = MDIO.scan(root, recursive=recursive, limit=int(limit))
        counts: dict = {}
        for item in items:
            counts[item.modality] = counts.get(item.modality, 0) + 1
        span = MDIO.common_span(items)
        return {"root": str(Path(root).resolve()), "n_items": len(items),
                "modalities": counts, "t_span": list(span) if span[0] is not None else None,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
                "items": [self._item_row(i) for i in items[:200]]}

    def _item_row(self, item) -> dict:
        return {
            "uri": str(item.uri), "name": Path(str(item.uri)).name, "modality": item.modality,
            "title": item.title or item.id, "n_views": item.n_views,
            "shape": list(item.shape or ()), "dtype": item.dtype,
            "t_ref": item.t_ref, "t_end": item.t_end,
            "channels": (item.meta or {}).get("channels") or [],
            "has_data": bool(item.has_data),
        }

    # ------------------------------------------------------------------ 条目与进度

    def items(self, dataset_id: str, version_id: str = "", state: str = "",
              modality: str = "") -> list:
        version_id = self.head_version(dataset_id, version_id)
        grouped: dict = {}
        for row in self.store.list_annotations(version_id):
            if state and row["state"] != state:
                continue
            grouped.setdefault(row["item_uri"], []).append(row)
        dataset = self.store.get_dataset(dataset_id) or {}
        # 必须是"磁盘 ∪ 已标注"：只标注过一条就把没标的文件挤出列表，
        # 用户就不知道还剩多少活了。已标注但磁盘上已不存在的条目也要保留，
        # 否则删文件会连带"删掉"标注记录在界面上的可见性。
        uris = sorted(set(grouped) | set(self._disk_uris(dataset, "")))
        out = []
        for uri in uris:
            kind = _safe_sniff(uri)
            if modality and kind != modality:
                continue
            anns = grouped.get(uri, [])
            out.append({
                "uri": uri, "name": Path(uri).name, "modality": kind,
                "n_annotations": len(anns),
                "states": {s: sum(1 for a in anns if a["state"] == s) for s in ST.STATES},
                "sources": sorted({a["ann"].source for a in anns}),
                "labels": sorted({a["ann"].label for a in anns if a["ann"].label}),
                "machine_only": bool(anns) and all(a["ann"].source != "manual" for a in anns),
            })
        return out

    def _disk_uris(self, dataset: dict, modality: str) -> list:
        root = Path(dataset.get("root") or ".")
        if not root.is_dir():
            return []
        return [str(i.uri) for i in self._scan_items(root, limit=100_000)
                if not modality or i.modality == modality]

    def stats(self, dataset_id: str, version_id: str = "") -> dict:
        version_id = self.head_version(dataset_id, version_id)
        rows = self.store.list_annotations(version_id)
        states: dict = {}
        sources: dict = {}
        labels: dict = {}
        for row in rows:
            states[row["state"]] = states.get(row["state"], 0) + 1
            sources[row["ann"].source] = sources.get(row["ann"].source, 0) + 1
            if row["ann"].label:
                labels[row["ann"].label] = labels.get(row["ann"].label, 0) + 1
        uris = {r["item_uri"] for r in rows}
        total = len(self._disk_uris(self.store.get_dataset(dataset_id) or {}, ""))
        return {"version_id": version_id, "n_annotations": len(rows),
                "n_items": len(uris), "n_items_on_disk": total,
                "labeled_ratio": round(len(uris) / total, 4) if total else 0.0,
                "states": states, "sources": sources,
                "labels": dict(sorted(labels.items(), key=lambda kv: -kv[1])),
                "n_reviews": len(self.store.review_log(version_id=version_id))}

    # ------------------------------------------------------------------ 渲染

    def frame(self, uri: str, params: Optional[dict] = None) -> dict:
        """一个数据项在当前视图参数下的一帧。渲染结果按参数组合缓存。"""
        params = dict(params or {})
        sl = slice_of(params)
        size = (int(params.get("width") or 960), int(params.get("height") or 540))
        style = style_of(params)
        key = self._frame_key(uri, sl, size, style, bool(params.get("mosaic")))
        cached = self._frames.get(key)
        if cached is not None:
            return {**cached, "cached": True}

        item = self.load_item(uri, modality=params.get("modality") or None)
        cursor = params.get("cursor")
        result = RD.render_item(item, size=size, window=sl, style=style,
                               cursor_t=float(cursor) if cursor not in (None, "") else None)
        body = {
            "uri": str(uri), "modality": item.modality, "png": png_b64(result.rgb),
            "width": size[0], "height": size[1],
            "title": result.title, "subtitle": result.subtitle,
            "info": _plain(result.info or {}),
            "index": sl.index, "t0": None if not np.isfinite(sl.t0) else sl.t0,
            "t1": None if not np.isfinite(sl.t1) else sl.t1,
            "n_views": item.n_views, "t_ref": item.t_ref, "t_end": item.t_end,
            "channels": (item.meta or {}).get("channels") or [],
            "units": (item.meta or {}).get("units") or [],
            "discrete": (item.meta or {}).get("discrete") or [],
            "cached": False,
        }
        self._remember(key, body)
        return body

    @staticmethod
    def _frame_key(uri, sl: Slice, size, style, mosaic: bool) -> str:
        payload = json.dumps([str(uri), sl.index,
                              None if not np.isfinite(sl.t0) else sl.t0,
                              None if not np.isfinite(sl.t1) else sl.t1,
                              list(size), sorted(style.items()), bool(mosaic)],
                             ensure_ascii=False, default=str)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def _remember(self, key: str, body: dict) -> None:
        """只留最近 FRAME_CACHE 帧：缓存无限增长的话，拖动一次就把内存吃光了。"""
        with self._lock:
            self._frames[key] = body
            self._frame_order.append(key)
            while len(self._frame_order) > FRAME_CACHE:
                self._frames.pop(self._frame_order.pop(0), None)

    def clear_cache(self) -> None:
        with self._lock:
            self._frames.clear()
            self._frame_order.clear()
            self._loaded.clear()
            self._loaded_order.clear()
            self._scans.clear()

    def frame_png(self, uri: str, params: Optional[dict] = None) -> tuple:
        """同一帧的 PNG 字节 + 元信息。

        网页里 `<img src="/api/frame?...">` 直接可用：浏览器自己缓存、自己并发，
        时间轴拖动时不必把 base64 塞进 JSON 再解一遍。元信息（`fit`）给画布做
        源像素↔屏幕像素的映射，标注和图才能对齐。
        """
        body = self.frame(uri, params)
        meta = {k: v for k, v in body.items() if k != "png"}
        return base64.b64decode(body["png"]), meta

    def thumbnails(self, uris: Sequence, cell: tuple = DEFAULT_CELL,
                   workers: int = 0) -> dict:
        """管理台的缩略图墙：并发出图，一路失败不拖垮整批。"""
        cell = (int(cell[0]), int(cell[1]))
        uris = [str(u) for u in uris]
        n = max(1, min(int(workers) or 4, 8, len(uris) or 1))

        def one(uri: str) -> dict:
            try:
                body = self.frame(uri, {"width": cell[0], "height": cell[1]})
                return {"uri": uri, "png": body["png"], "modality": body["modality"],
                        "subtitle": body["subtitle"]}
            except Exception as exc:      # noqa: BLE001 - 一张坏图不该让整页打不开
                return {"uri": uri, "png": "", "error": f"{type(exc).__name__}: {exc}"}

        t0 = time.perf_counter()
        if n == 1:
            rows = [one(u) for u in uris]
        else:
            with ThreadPoolExecutor(max_workers=n) as pool:
                rows = list(pool.map(one, uris))
        return {"thumbs": rows, "workers": n,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
                "n_errors": sum(1 for r in rows if r.get("error"))}

    def mosaic(self, uris: Sequence, params: Optional[dict] = None) -> dict:
        params = dict(params or {})
        uris = [str(u) for u in uris]
        items = [MDIO.load_any(u) for u in uris]
        cell = (int(params.get("cell_width") or 320), int(params.get("cell_height") or 200))
        cols = int(params.get("cols") or 0) or max(1, min(len(items), 4))
        rows = int(params.get("rows") or 0) or max(1, math.ceil(len(items) / cols))
        results = [RD.render_item(i, size=cell, window=slice_of(params),
                                  style=style_of(params)) for i in items]
        rgb = RD.render_mosaic(results, rows=rows, cols=cols, cell=cell)
        return {"png": png_b64(rgb), "n_items": len(items), "rows": rows, "cols": cols}

    # ------------------------------------------------------------------ 分析与后端

    def analyze(self, uri: str, analyzers: Sequence, params: Optional[dict] = None) -> dict:
        """跑算子并把叠加层摊平成标注几何——网页画布和后端预览图共用同一套数。"""
        from .. import analysis

        params = dict(params or {})
        item = MDIO.load_any(str(uri), modality=params.get("modality") or None)
        sl = slice_of(params)
        out = {"uri": str(uri), "modality": item.modality, "metrics": {}, "flags": [],
               "anns": [], "errors": {}}
        for name in analyzers:
            if name not in analysis.names():
                out["errors"][name] = f"未知算子（可用 {analysis.names()}）"
                continue
            inst = analysis.get(name, **(params.get("analyzer_params") or {}))
            if not inst.handles(item):
                out["errors"][name] = f"{name} 不处理 {item.modality}"
                continue
            res = inst.run(item, sl)
            for k, v in res.metrics.items():
                out["metrics"][f"{name}.{k}"] = _plain(v)
            out["flags"].extend({"analyzer": name, "t": f.t, "label": f.label,
                                 "score": _plain(f.score), "channel": f.channel,
                                 "level": f.level} for f in res.flags[:50])
            out["anns"].extend(_plain(a.to_json()) for a in
                               _overlay_anns(res.overlay, name, sl.index))
        return out

    def backends(self, domain: str = "", task: str = "", modality: str = "") -> list:
        from ..backends.base import catalog

        return catalog(domain=domain, task=task, modality=modality)

    def probe(self) -> dict:
        from ..backends.base import BACKENDS

        return {**probe_raw().to_json(), "registered": sorted(BACKENDS.names())}

    def recommend(self, domain: str = "", task: str = "", k: int = 3) -> list:
        from ..backends.recommend import top3

        return top3(domain, task, probe_raw(), self.bench_table(), k=max(1, int(k)))

    def gate_check(self, backend: str, task: str = "", domain: str = "",
                   threshold: float = 0.90) -> dict:
        from ..backends.recommend import usable

        ok, f1, why = usable(backend, task, domain, float(threshold), self.bench_table())
        return {"backend": backend, "usable": ok, "f1": _plain(f1), "reason": why,
                "threshold": float(threshold)}

    def bench(self, root, categories: str = "", only: str = "", limit: int = 4,
              include_stubs: bool = False) -> dict:
        from .. import backends as BE

        rows = BE.bench_run(Path(root), categories=[c for c in categories.split(",") if c] or None,
                           only=[o for o in only.split(",") if o] or None,
                           limit=int(limit), include_stubs=bool(include_stubs))
        BE.bench_save(rows, self.out_dir / "bench" / "backends.json")
        return {"rows": [r.to_json() for r in rows], "n": len(rows),
                "table": str(self.out_dir / "bench" / "backends.json")}

    def bench_table(self) -> list:
        from .. import backends as BE

        path = self.out_dir / "bench" / "backends.json"
        return BE.load_table(path) if path.is_file() else []

    def operators(self, kind: str = "analysis") -> list:
        """把可选算子/后端/清洗/增强项列给前端生成参数面板。"""
        if kind == "analysis":
            from .. import analysis
            from ..core.registry import ANALYZERS

            return [{"name": n, "description": ANALYZERS.get(n).description,
                     "schema": _plain(ANALYZERS.get(n).schema)} for n in analysis.names()]
        if kind == "clean":
            from ..clean.ops import CLEAN_OPS

            return [{"name": n, "description": CLEAN_OPS.get(n).description,
                     "schema": _plain(CLEAN_OPS.get(n).schema)} for n in CLEAN_OPS.names()]
        if kind == "augment":
            from ..augment.ops import AUG_OPS
            from ..augment.policy import presets

            return [{"name": n, "description": AUG_OPS.get(n).description,
                     "schema": _plain(AUG_OPS.get(n).schema)} for n in AUG_OPS.names()], \
                   presets()
        if kind == "export":
            from ..export.base import EXPORTERS
            from .. import export as EXP

            return [{"name": n, "description": EXPORTERS.get(n).description,
                     "schema": _plain(EXPORTERS.get(n).schema)} for n in EXP.formats()]
        raise ValueError(f"未知算子类别 {kind!r}，可用 analysis/clean/augment/export")

    # ------------------------------------------------------------------ 标注与审核

    def annotations(self, version_id: str, uri: str = "") -> list:
        rows = self.store.list_annotations(version_id, uri or None)
        return [{"id": r["id"], "uri": r["item_uri"], "state": r["state"], "role": r["role"],
                 "ts": r["ts"], "ann": _plain(r["ann"].to_json())} for r in rows]

    def put_annotations(self, version_id: str, uri: str, anns: Sequence, role: str = "",
                        state: str = "draft", replace: bool = True) -> dict:
        """整条目的标注写入：先删旧的再写新的（`replace=True`），删除会留 review 痕迹。

        替换而不是逐条对账，是因为网页端提交的是"这一屏的最终结果"——
        对账要比较几何等价，界面反而更容易出错。
        """
        ids = []
        removed = 0
        if replace:
            for row in self.store.list_annotations(version_id, uri):
                if row["state"] in ("approved",):
                    raise PermissionError(
                        f"{uri} 有已通过的标注（{row['id']}），不许被整条替换；先驳回再改")
                self.store.delete_annotation(row["id"], role or "admin", "replace")
                removed += 1
        for payload in anns or []:
            ann = payload["ann"] if isinstance(payload, dict) and "ann" in payload else payload
            ann = ann if isinstance(ann, Ann) else Ann.from_json(ann)
            ids.append(self.store.put_annotation(version_id, uri, ann, role=role, state=state))
        return {"version_id": version_id, "uri": uri, "ids": ids, "removed": removed,
                "n": len(ids)}

    def set_state(self, annotation_id: str, role: str, action: str, reason: str = "") -> dict:
        to = self.store.set_state(annotation_id, role, action, reason)
        return {"annotation_id": annotation_id, "state": to, "role": role, "action": action}

    def submit_item(self, version_id: str, uri: str, role: str = "annotator") -> dict:
        moved = self.store.submit_many(version_id, uri, role)
        return {"uri": uri, "n_submitted": moved}

    def review_bulk(self, version_id: str, role: str = "reviewer", action: str = "approve",
                    reason: str = "", max_confidence: Optional[float] = None) -> dict:
        """批量验收只动 submitted 状态的行；`max_confidence` 给的就是"只收高置信预标注"。"""
        moved = self.store.review_bulk(version_id, role, action, reason, max_confidence)
        return {"n": moved, "action": action}

    def queue(self, version_id: str, role: str = "reviewer") -> list:
        return self.store.queue(version_id, role)

    def review_log(self, annotation_id: str = "", version_id: str = "") -> list:
        return self.store.review_log(annotation_id or None, version_id or None)

    def progress(self, version_id: str) -> dict:
        return _plain(self.store.progress(version_id))

    def diff(self, v1: str, v2: str) -> dict:
        return _plain(self.store.diff(v1, v2))

    def lineage(self, version_id: str) -> list:
        return _plain(self.store.lineage(version_id))

    def new_version(self, dataset_id: str, parent_id: str = "", op: str = "manual",
                    params: Optional[dict] = None, carry: bool = True) -> dict:
        """手动另存一版（界面上的"存为新版"）。父版本缺省取该数据集最新一版。"""
        parent = parent_id or self.store.head_version(dataset_id)
        vid = self.store.put_version(dataset_id, parent, op, params or {})
        n = 0
        if carry and parent:
            n = _inherit(self.store, parent, vid)
        return {"version_id": vid, "parent": parent, "op": op, "n_carried": n}

    # ------------------------------------------------------------------ 智能标注

    def prompt_from(self, params: dict):
        """查询/请求体里的提示 -> Prompt。带 source_uri 时把模板钉死，跨图才可比。"""
        from ..annotate.prompts import resolve_prompt
        from ..backends.base import Prompt

        prompt = Prompt(params.get("kind", "box"), params.get("coords") or [],
                        params.get("label", ""), params.get("role", "positive"),
                        params.get("source_uri", ""))
        if prompt.kind in ("box", "poly", "point") and prompt.source_uri:
            return resolve_prompt(prompt.source_uri, prompt, params)
        return prompt

    def same_image(self, uri: str, params: dict) -> dict:
        from ..annotate.prompts import same_image

        item = MDIO.load_any(str(uri))
        anns = same_image(item, self.prompt_from(params), params.get("backend", "visual_prompt"),
                          params.get("backend_params") or {},
                          window=slice_of(params))
        return {"uri": uri, "anns": [_plain(a.to_json()) for a in anns], "n": len(anns)}

    def preannotate(self, uris: Sequence, params: dict, apply_to: str = "") -> dict:
        """整批预标注。`apply_to` 给了版本 id 就落库并产新版本（不覆盖人工标注）。"""
        from ..annotate.prompts import apply_results, preannotate

        result = preannotate([str(u) for u in uris], params.get("backend", "cosmic_reject"),
                             params.get("backend_params") or {},
                             workers=int(params.get("workers") or 0),
                             executor=params.get("executor", "thread"),
                             prompts=[self.prompt_from(params)] if params.get("coords") else ())
        payload = {"n_items": result["n_items"], "n_annotations": result["n_annotations"],
                   "n_ok": result["n_ok"], "errors": result["errors"],
                   "speedup": result["speedup"], "workers": result["workers"],
                   "by_uri": {k: [_plain(a.to_json()) for a in v]
                              for k, v in result["results"].items()}}
        if apply_to:
            written = apply_results(self.store, apply_to, result,
                                    role=params.get("role", "annotator"),
                                    state=params.get("state", "draft"))
            payload.update({"version_id": written["version"], "parent": apply_to,
                            "n_written": written["added"], "deduped": written["deduped"],
                            "carried": written.get("carried", 0),
                            "task_id": written.get("task_id", "")})
        return payload

    def undo_machine(self, version_id: str) -> dict:
        from ..annotate.prompts import undo_last

        return _plain(undo_last(self.store, version_id))

    def speedup(self, uris: Sequence, params: dict) -> dict:
        from ..annotate.prompts import measure_speedup

        return _plain(measure_speedup([str(u) for u in uris],
                                      params.get("backend", "cosmic_reject"),
                                      params.get("backend_params") or {},
                                      workers=int(params.get("workers") or 4),
                                      executor=params.get("executor", "thread")))

    def hard_examples(self, version_id: str, uris: Sequence, params: dict) -> dict:
        from ..annotate.active import hard_examples

        return _plain(hard_examples(self.store, version_id, [str(u) for u in uris],
                                   params.get("backend", "cosmic_reject"),
                                   k=int(params.get("k") or 20),
                                   params=params.get("backend_params") or {}))

    def active_round(self, version_id: str, uris: Sequence, params: dict) -> dict:
        """选下一轮该标哪些（不确定性排序），只读不落库。"""
        from ..annotate.active import pick_round, uncertainty, stop_advice

        ranked = uncertainty([str(u) for u in uris], params.get("backend", "cosmic_reject"),
                             params.get("backend_params") or {})
        budget = int(params.get("budget") or 20)
        chosen = pick_round(ranked, budget, self.store.annotated_items(version_id))
        return {"ranked": ranked[:max(20, budget)], "chosen": chosen, "budget": budget,
                "advice": stop_advice(self.round_history(version_id))}

    def round_history(self, version_id: str) -> list:
        return [t["report"] for t in self.store.list_tasks(kind="active")
                if t["version_in"] == version_id]

    def active_run(self, version_id: str, uris: Sequence, params: dict) -> dict:
        """跑多轮主动学习曲线（用真值当模拟标注员，给论文 E3 出数）。

        `oracle` 是模拟的，界面里真人标注不会走这条路；准入门槛默认照生效，
        所以后端实测 F1 不够时这里会直接返回"禁用 + 实测值"，和网页上的提示一致。
        """
        from ..annotate.active import SimulatedAnnotator, run_loop

        truth = _truth_for(self._dataset_root_of_version(version_id))
        task = params.get("task", "reject_cosmic")
        backend = params.get("backend", "cosmic_reject")
        oracle = SimulatedAnnotator(truth, task,
                                   drop=float(params.get("drop") or 0.0),
                                   jitter_px=float(params.get("jitter_px") or 0.0),
                                   seed=int(params.get("seed") or 0))
        table = params.get("table")
        if table is None:
            table = self.bench_table()
        return _plain(run_loop(self.store, version_id, [str(u) for u in uris], backend, oracle,
                              truth, task, domain=params.get("domain", ""),
                              rounds=int(params.get("rounds") or 4),
                              budget=int(params.get("budget") or 5),
                              params=params.get("backend_params") or {},
                              min_f1=float(params.get("min_f1") or 0.90), table=table,
                              workers=int(params.get("workers") or 0),
                              enforce_gate=bool(params.get("enforce_gate", True))))

    def _dataset_root_of_version(self, version_id: str) -> Path:
        version = self.store.get_version(version_id)
        if version is None:
            raise KeyError(f"未知版本 {version_id}")
        dataset = self.store.get_dataset(version["dataset_id"]) or {}
        return Path(dataset.get("root") or ".")

    # ------------------------------------------------------------------ 清洗与增强

    def clean_preview(self, uris: Sequence, specs: Sequence, calibration: str = "") -> dict:
        from .. import clean as CL

        return _plain(CL.preview(CL.load_items([str(u) for u in uris]), specs,
                                 _calibration_or_none(calibration)))

    def clean_apply(self, version_id: str, specs: Sequence, uris: Sequence = (),
                    calibration: str = "", write_transformed: bool = False,
                    author: str = "") -> dict:
        from .. import clean as CL

        report = CL.apply(self.store, version_id, specs,
                          uris=[str(u) for u in uris] or None, out_dir=self.out_dir,
                          calibration=_calibration_or_none(calibration),
                          write_transformed=bool(write_transformed), author=author)
        self._invalidate_scans()      # 清洗可能删/写文件，条目列表必须立刻反映
        return _plain({**report, "version_id": report["version"]})

    def clean_rollback(self, version_id: str) -> dict:
        from .. import clean as CL

        out = _plain(CL.rollback(self.store, version_id))
        self._invalidate_scans()
        return out

    def clean_dropped(self, version_id: str) -> list:
        from .. import clean as CL

        return _plain(CL.dropped_of(self.store, version_id))

    def augment_check(self, uris: Sequence, recipe: dict, category: str = "") -> dict:
        from .. import augment as AUG

        items = [MDIO.load_any(str(u)) for u in uris]
        return {"warnings": AUG.check(AUG.Recipe.from_obj(recipe), items, category=category)}

    def augment_run(self, version_id: str, recipe: dict, uris: Sequence = (),
                    author: str = "") -> dict:
        from .. import augment as AUG

        report = AUG.run(self.store, version_id, AUG.Recipe.from_obj(recipe),
                         out_dir=self.out_dir, uris=[str(u) for u in uris] or None,
                         author=author)
        self._invalidate_scans()      # 增强写盘后条目列表要能看到新文件
        return _plain({**report, "version_id": report["version"]})

    def augment_replay(self, version_id: str) -> dict:
        from .. import augment as AUG

        return _plain(AUG.replay(self.store, version_id))

    def augment_lineage(self, version_id: str) -> list:
        """逐条增强谱系：源文件 → 写了什么 → 参数链。界面上"这条从哪来"就看这个。"""
        from .. import augment as AUG

        return _plain(AUG.read_lineage(self.store, version_id))

    # ------------------------------------------------------------------ 导出

    def export(self, version_id: str, fmt: str, params: Optional[dict] = None,
               out_dir: str = "", make_scripts: bool = False) -> dict:
        from .. import export as EXP

        return _plain(EXP.export(self.store, version_id, fmt,
                                 out_dir=out_dir or str(self.out_dir / "export" / fmt),
                                 make_scripts=bool(make_scripts), **(params or {})))

    def export_validate(self, version_id: str, directory: str) -> dict:
        from .. import export as EXP
        from ..export.validate import summarize

        check = EXP.validate_export(directory, version_id, self.store)
        return {"ok": check["ok"], "summary": summarize(check), "check": _plain(check)}

    def export_report(self, version_id: str) -> list:
        return [t for t in self.store.list_tasks(kind="export") if t["version_in"] == version_id]

    # ------------------------------------------------------------------ 收录（文件搬运）

    def collect(self, uris: Sequence, target, mode: str = "copy",
                flatten: bool = False) -> dict:
        """把选中的条目收进数据集目录。清单写在目标目录里，`collect_undo` 按清单回退。

        move 模式真的会挪原件，所以界面必须显式选它；缺省 copy，源目录保持不动。
        """
        from .. import collect as COL

        items = [MDIO.load_any(str(u)) for u in uris]
        got = COL.collect(items, Path(target), mode=mode, flatten=bool(flatten))
        self._invalidate_scans()      # move 模式会挪原件，copy 会给目标目录添新文件
        return {"target": got.target, "mode": got.mode, "n_entries": len(got.entries),
                "n_skipped": len(got.skipped), "n_created": len(got.created),
                "manifest": str(got.manifest_path),
                "entries": [{"src": e.src, "dst": e.dst} for e in got.entries[:200]]}

    def collect_undo(self, manifest_path: str, keep_dirs: bool = True,
                     force: bool = False) -> dict:
        from .. import collect as COL

        out = _plain(COL.undo(Path(manifest_path), keep_dirs=bool(keep_dirs),
                              force=bool(force)))
        self._invalidate_scans()
        return out

    def collect_latest(self, target) -> dict:
        from .. import collect as COL

        found = COL.latest_manifest(Path(target))
        return {"manifest": str(found) if found else "",
                "exists": bool(found)}


def _inherit(store, from_version: str, to_version: str) -> int:
    from ..clean.task import inherit

    return inherit(store, from_version, to_version)


def _calibration_or_none(path: str):
    if not path:
        return None
    from ..io.series import Calibration

    return Calibration.load(Path(path))


def _truth_for(root: Path) -> dict:
    """数据集目录旁的 truth.json——主动学习的模拟标注员和精度曲线都靠它。"""
    path = Path(root) / "truth.json"
    if not path.is_file():
        raise FileNotFoundError(f"没有真值文件 {path}，主动学习循环与精度曲线都跑不了")
    return json.loads(path.read_text(encoding="utf-8"))
