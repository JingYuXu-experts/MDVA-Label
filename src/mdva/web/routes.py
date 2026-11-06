"""路由表：`(方法, 路径) -> 处理函数`，与传输层无关。

为什么单独一层：装 fastapi/uvicorn 是用户自己的事，而这套接口必须今天就跑起来，
所以路由写成纯函数表，`server.py`（标准库）和任何别的适配器都只是执行它的壳。
测试直接调 `call()`，不建 socket 也能把整张表跑一遍。

约定：
- uri / 数据集 id / 版本 id 一律走查询参数或请求体，不做 URL 路径段——Windows 路径里
  有 `:` 和 `/`，塞进路径段要两头编码，前端更容易写错。
- 处理函数返回 dict/list 就是 JSON；返回 `Image` 就是原始字节（PNG），
  浏览器可以直接 `<img src="/api/frame?...">`，拖动时间轴时靠浏览器缓存，不必反复解 base64。
- 参数缺失或非法 → 400；角色越权 → 403；id 不存在 → 404。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from .deps import (Workspaces, UnknownWorkspace, resolve_dataset, resolve_version)
from .services import DEFAULT_CELL, Hub

API_PREFIX = "/api"

#: 路由自己要用的键：`rest()` 把它们挡在下层参数之外，免得 `version` 混进算法参数里
ROUTING_KEYS = ("ws", "dataset", "version", "uris", "uri", "params", "anns", "ann")


# ---------------------------------------------------------------------- 错误与响应

class Bad(ValueError):
    """请求本身有问题（缺参数、类型不对、组合不合法）→ 400。"""


class Missing(KeyError):
    """id 指向的东西不存在 → 404。"""


@dataclass
class Image:
    """原始字节响应（PNG）。`meta` 里的小字段进响应头，方便对着浏览器调试。"""
    body: bytes
    content_type: str = "image/png"
    meta: dict = field(default_factory=dict)


@dataclass
class Response:
    status: int = 200
    json_body: Any = None
    data: Optional[bytes] = None
    content_type: str = "application/json; charset=utf-8"
    headers: dict = field(default_factory=dict)

    @staticmethod
    def of(value, status: int = 200) -> "Response":
        if isinstance(value, Image):
            heads = {"Content-Type": value.content_type}
            for k, v in value.meta.items():
                heads[f"X-Mdva-{k}"] = str(v)
            return Response(status, None, value.body, value.content_type, heads)
        return Response(status, value)


# ---------------------------------------------------------------------- 参数读取

class Params:
    """查询串 + JSON 请求体合成的只读参数视图，界面和命令行都能用同一套写法。"""

    def __init__(self, query: Optional[dict] = None, body: Optional[dict] = None):
        self._q = {}
        for key, value in (query or {}).items():
            self._q[str(key)] = value[-1] if isinstance(value, list) and value else value
        self._b = dict(body or {})

    def has(self, key: str) -> bool:
        return key in self._b or key in self._q

    def raw(self, key: str, default=None):
        if key in self._b:
            return self._b[key]
        value = self._q.get(key, default)
        return default if value is None or value == "" else value

    def text(self, key: str, default: str = "") -> str:
        value = self.raw(key, default)
        return "" if value is None else str(value)

    def need(self, key: str) -> str:
        value = self.text(key).strip()
        if not value:
            raise Bad(f"缺少参数 {key}")
        return value

    def int_(self, key: str, default: int = 0) -> int:
        value = self.raw(key, default)
        try:
            return int(float(value))
        except (TypeError, ValueError):
            raise Bad(f"参数 {key} 要的是整数，收到 {value!r}") from None

    def float_(self, key: str, default: Optional[float] = None) -> Optional[float]:
        value = self.raw(key, None)
        if value in (None, ""):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            raise Bad(f"参数 {key} 要的是数字，收到 {value!r}") from None

    def flag(self, key: str, default: bool = False) -> bool:
        value = self.raw(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    def list_(self, key: str, default: Optional[list] = None) -> list:
        """列表参数：请求体里是真数组，查询串里可以是 JSON 或逗号分隔。

        Windows 路径里可能真有逗号，所以前端传数组、逗号分隔只当命令行式的便利写法。
        """
        value = self.raw(key, None)
        if value is None:
            return list(default or [])
        if isinstance(value, (list, tuple)):
            return list(value)
        text = str(value).strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                raise Bad(f"参数 {key} 不是合法的 JSON 数组：{text[:60]}") from None
            return list(parsed) if isinstance(parsed, list) else [parsed]
        return [t for t in text.split(",") if t]

    def dict_(self, key: str, default: Optional[dict] = None) -> dict:
        value = self.raw(key, None)
        if value is None:
            return dict(default or {})
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                raise Bad(f"参数 {key} 不是合法的 JSON 对象") from None
        if not isinstance(value, dict):
            raise Bad(f"参数 {key} 要的是对象，收到 {type(value).__name__}")
        return dict(value)

    def pair(self, key: str, default: tuple = DEFAULT_CELL) -> tuple:
        """缩略图边长一类的成对参数：`196x122`、`[196,122]`、`196,122` 都认。"""
        value = self.raw(key, None)
        if value is None:
            return tuple(default)
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return (int(value[0]), int(value[1]))
        text = str(value).lower().replace(" ", "")
        parts = text.replace("x", ",").split(",")
        if len(parts) != 2:
            raise Bad(f"参数 {key} 要形如 196x122，收到 {value!r}")
        try:
            return (max(1, int(float(parts[0]))), max(1, int(float(parts[1]))))
        except ValueError:
            raise Bad(f"参数 {key} 要形如 196x122，收到 {value!r}") from None

    def anns(self, key: str = "anns") -> list:
        """标注列表：既接受 `[{geom,coords,...}]`，也接受 `{anns:{...}}` 单条写法。"""
        value = self.raw(key, [])
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise Bad(f"参数 {key} 要是标注数组")
        return list(value)

    def rest(self, exclude: Sequence = ROUTING_KEYS) -> dict:
        """路由自己不用的其余参数，原样给下层（services 各方法自己按名字取）。"""
        out = {}
        for key in self._b:
            if key not in exclude:
                out[key] = self._b[key]
        for key in self._q:
            if key not in exclude and key not in out:
                out[key] = self._q[key]
        return out


# ---------------------------------------------------------------------- 上下文

class Ctx:
    """一次请求：工作区注册表 + 当前 Hub + 参数，外加数据集/版本的按需解析。"""

    def __init__(self, workspaces: Workspaces, hub: Hub, p: Params, ws: str):
        self.ws = workspaces
        self.hub = hub
        self.p = p
        self.ws_name = ws
        self._dataset: Optional[dict] = None
        self._version: Optional[str] = None

    def dataset(self) -> dict:
        if self._dataset is None:
            try:
                self._dataset = resolve_dataset(self.hub, self.p.text("dataset"))
            except KeyError as exc:
                raise Missing(str(exc.args[0]) if exc.args else "数据集不明确") from None
        return self._dataset

    def dataset_id(self) -> str:
        return self.dataset()["id"]

    def version_id(self) -> str:
        if self._version is None:
            try:
                self._version = resolve_version(self.hub, self.dataset_id(),
                                                self.p.text("version"))
            except KeyError as exc:
                raise Missing(str(exc.args[0]) if exc.args else "版本不存在") from None
        return self._version


# ---------------------------------------------------------------------- 路由表

Handler = Callable[[Ctx], Any]


@dataclass
class Route:
    method: str
    path: str
    handler: Handler
    doc: str = ""


ROUTES: dict[tuple, Route] = {}


def route(method: str, path: str, doc: str = ""):
    def deco(fn: Handler) -> Handler:
        key = (method.upper(), path)
        if key in ROUTES:
            raise ValueError(f"重复注册路由 {key}")
        ROUTES[key] = Route(key[0], path, fn, doc or (fn.__doc__ or "").strip().split("\n")[0])
        return fn
    return deco


def _split_analyzers(p: Params) -> list:
    """`analyzers` 缺省跑全部：界面上的"一键体检"就是什么都不填。"""
    from .. import analysis

    names = p.list_("analyzers") or p.list_("analyzer")
    return list(names) if names else list(analysis.names())


# ---------------------------------------------------------------------- 元信息

@route("GET", "/health", "服务与进程内环境")
def _health(cx: Ctx):
    from .. import __version__

    return {"ok": True, "app": "mdva", "version": __version__, "root": str(cx.ws.root),
            "workspaces": cx.ws.names(), "default": cx.ws.default_ws(),
            "out_dir": str(cx.hub.out_dir), "offline": True}


@route("GET", "/workspaces", "列出扫描到的工作区与数据集")
def _workspaces(cx: Ctx):
    return {"workspaces": cx.ws.list()}


@route("POST", "/workspaces/rescan", "重扫目录：命令行新建的数据集不用重启服务")
def _rescan(cx: Ctx):
    rows = cx.ws.rescan()
    return {"n": len(rows), "workspaces": rows}


@route("GET", "/probe", "本机能力（CPU/内存/显卡档位）与已注册后端")
def _probe(cx: Ctx):
    return cx.hub.probe()


@route("GET", "/operators", "参数面板的元数据：analysis / clean / augment / export")
def _operators(cx: Ctx):
    kind = cx.p.text("kind", "analysis")
    if kind == "augment":
        ops, presets = cx.hub.operators("augment")
        return {"ops": ops, "presets": presets}
    return {"ops": cx.hub.operators(kind)}


# ---------------------------------------------------------------------- 数据集与条目

@route("GET", "/datasets", "当前工作区的数据集列表")
def _datasets(cx: Ctx):
    return {"datasets": cx.hub.datasets()}


@route("POST", "/datasets", "新建数据集：root 指向磁盘目录，labels 给类别表")
def _create_dataset(cx: Ctx):
    from ..annotate.store import DATASET_TYPES

    type_ = cx.p.text("type", "detection")
    if type_ not in DATASET_TYPES:
        raise Bad(f"未知数据集类型 {type_!r}，可用 {list(DATASET_TYPES)}")
    ds = cx.hub.create_dataset(cx.p.need("name"), type_, cx.p.need("root"),
                               category=cx.p.text("category"),
                               labels=cx.p.list_("labels"))
    return {**ds, "detail": cx.hub.dataset(ds["dataset_id"])}


@route("GET", "/dataset", "单个数据集的详情（含条目与版本统计）")
def _dataset(cx: Ctx):
    return cx.hub.dataset(cx.dataset_id())


@route("POST", "/labels", "整表替换类别定义（名称/颜色/层级）")
def _set_labels(cx: Ctx):
    labels = cx.p.list_("labels")
    if not labels:
        raise Bad("labels 是空表：删类别请改成新的完整列表，避免一键清空")
    return {"labels": cx.hub.set_labels(cx.dataset_id(), labels)}


@route("GET", "/versions", "版本 DAG（线性时间序，最新在最后）")
def _versions(cx: Ctx):
    return {"versions": cx.hub.versions(cx.dataset_id()), "head": cx.version_id()}


@route("GET", "/manifest", "导出版本清单（拓扑序，给人读的 manifest.json）")
def _manifest(cx: Ctx):
    return cx.hub.manifest(cx.dataset_id())


@route("GET", "/scan", "只读探测一个目录：建集之前先看看有多少能打开")
def _scan(cx: Ctx):
    root = cx.p.need("root")
    return cx.hub.scan(root, recursive=cx.p.flag("recursive", True),
                       limit=cx.p.int_("limit", 2000))


@route("GET", "/items", "数据集条目 + 各自的标注状态")
def _items(cx: Ctx):
    rows = cx.hub.items(cx.dataset_id(), cx.version_id(), state=cx.p.text("state"),
                        modality=cx.p.text("modality"))
    return {"items": rows, "n": len(rows)}


@route("GET", "/stats", "统计总览（模态分布、审核进度、标签计数）")
def _stats(cx: Ctx):
    return cx.hub.stats(cx.dataset_id(), cx.version_id())


# ---------------------------------------------------------------------- 出图

@route("GET", "/frame", "渲染一帧，直接给 PNG 字节（画布与缩略图都用它）")
def _frame(cx: Ctx):
    png, meta = cx.hub.frame_png(cx.p.need("uri"), _frame_params(cx.p))
    return Image(png, meta={"Modality": meta["modality"], "Width": meta["width"],
                            "Height": meta["height"], "Cached": int(meta["cached"])})


@route("GET", "/frame/meta", "同一帧的元信息（拉伸区间、fit 映射、通道表），不含图片")
def _frame_meta(cx: Ctx):
    png, meta = cx.hub.frame_png(cx.p.need("uri"), _frame_params(cx.p))
    meta = dict(meta)
    meta["png_bytes"] = len(png)
    return meta


@route("GET", "/thumbs", "缩略图墙：并发出图，一路坏图不影响其余")
def _thumbs(cx: Ctx):
    return cx.hub.thumbnails(cx.p.list_("uris"), cell=cx.p.pair("cell"),
                             workers=cx.p.int_("workers", 0))


@route("GET", "/mosaic", "多路同场景对比拼图，直接给 PNG")
def _mosaic(cx: Ctx):
    p = cx.p
    params = _frame_params(p)
    params["cell_width"], params["cell_height"] = p.pair("cell", (320, 200))
    return Image(_b64(cx.hub.mosaic(p.list_("uris"), params)["png"]))


def _frame_params(p: Params) -> dict:
    """把渲染相关参数原样交给 services.slice_of/style_of，字段名与前端一致。"""
    keys = ("index", "t0", "t1", "width", "height", "modality", "cursor",
            "interval", "stretch", "colormap", "gamma", "vmin", "vmax", "invert",
            "channel", "method", "ymin", "ymax", "out_width", "palette",
            "mosaic", "rows", "cols")
    out = {}
    for key in keys:
        if p.has(key):
            out[key] = p.raw(key)
    for key in ("width", "height", "index", "channel", "rows", "cols"):
        if key in out:
            out[key] = p.int_(key)
    return out


def _b64(text: str) -> bytes:
    import base64

    return base64.b64decode(text)


# ---------------------------------------------------------------------- 分析与后端

@route("POST", "/analyze", "对一路数据跑算子：指标、事件、可落库的叠加层")
def _analyze(cx: Ctx):
    params = cx.p.dict_("params")
    merged = {**params, **{k: cx.p.raw(k) for k in ("index", "t0", "t1", "channel",
                                                   "modality") if cx.p.has(k)}}
    return cx.hub.analyze(cx.p.need("uri"), _split_analyzers(cx.p), merged)


@route("GET", "/backends", "算法替换下拉框：后端清单 + 本机可用性")
def _backends(cx: Ctx):
    rows = cx.hub.backends(domain=cx.p.text("domain"), task=cx.p.text("task"),
                          modality=cx.p.text("modality"))
    return {"backends": rows, "n": len(rows)}


@route("GET", "/recommend", "按实测表排序的推荐（没实测表就返回空并说明）")
def _recommend(cx: Ctx):
    rows = cx.hub.recommend(domain=cx.p.text("domain"), task=cx.p.text("task"),
                           k=cx.p.int_("k", 3))
    return {"recommended": rows, "has_table": bool(cx.hub.bench_table()),
            "note": "" if rows else "还没有实测表：先跑 mdva bench 或在后端页点「开始实测」"}


@route("GET", "/gate", "指定后端是否达到准入门槛")
def _gate(cx: Ctx):
    return cx.hub.gate_check(cx.p.need("backend"), task=cx.p.text("task"),
                            domain=cx.p.text("domain"),
                            threshold=cx.p.float_("threshold", 0.90))


@route("GET", "/bench", "读回实测表")
def _bench_table(cx: Ctx):
    return {"rows": cx.hub.bench_table()}


@route("POST", "/bench", "跑实测并落表（精度/延迟/推荐）")
def _bench(cx: Ctx):
    return cx.hub.bench(cx.p.need("root"), categories=cx.p.text("categories"),
                        only=",".join(str(o) for o in cx.p.list_("only")),
                        limit=cx.p.int_("limit", 4),
                        include_stubs=cx.p.flag("stubs"))


# ---------------------------------------------------------------------- 标注与审核

@route("GET", "/annotations", "某版本（可限某一路）的全部标注")
def _annotations(cx: Ctx):
    rows = cx.hub.annotations(cx.p.text("version") or cx.version_id(), cx.p.text("uri"))
    return {"annotations": rows, "n": len(rows)}


@route("POST", "/annotations", "整条覆盖式写入：界面提交的是「这一屏的最终结果」")
def _put_annotations(cx: Ctx):
    return cx.hub.put_annotations(cx.p.text("version") or cx.version_id(),
                                  cx.p.need("uri"), cx.p.anns(),
                                  role=cx.p.text("role"), state=cx.p.text("state", "draft"),
                                  replace=cx.p.flag("replace", True))


@route("POST", "/annotations/state", "单条状态迁移（角色 + 动作，越权 403）")
def _set_state(cx: Ctx):
    return cx.hub.set_state(cx.p.need("annotation_id"), cx.p.need("role"),
                            cx.p.need("action"), reason=cx.p.text("reason"))


@route("POST", "/annotations/submit", "整条提交审核")
def _submit(cx: Ctx):
    return cx.hub.submit_item(cx.p.text("version") or cx.version_id(), cx.p.need("uri"),
                              role=cx.p.text("role", "annotator"))


@route("POST", "/annotations/review", "批量验收/驳回，可按置信度只收机器预标注")
def _review(cx: Ctx):
    return cx.hub.review_bulk(cx.p.text("version") or cx.version_id(),
                              role=cx.p.text("role", "reviewer"),
                              action=cx.p.text("action", "approve"),
                              reason=cx.p.text("reason"),
                              max_confidence=cx.p.float_("max_confidence"))


@route("GET", "/queue", "某角色的待办队列")
def _queue(cx: Ctx):
    vid = cx.p.text("version") or cx.version_id()
    role = cx.p.text("role", "reviewer")
    return {"version_id": vid, "role": role, "queue": cx.hub.queue(vid, role)}


@route("GET", "/progress", "标注进度与审核分布")
def _progress(cx: Ctx):
    return cx.hub.progress(cx.p.text("version") or cx.version_id())


@route("GET", "/review_log", "谁在什么时候改了什么（审计线索）")
def _review_log(cx: Ctx):
    return {"log": cx.hub.review_log(annotation_id=cx.p.text("annotation_id"),
                                    version_id=cx.p.text("version"))}


@route("GET", "/diff", "两个版本的标注差异")
def _diff(cx: Ctx):
    return cx.hub.diff(cx.p.need("v1"), cx.p.need("v2"))


@route("GET", "/lineage", "版本谱系（父链）")
def _lineage(cx: Ctx):
    return {"lineage": cx.hub.lineage(cx.p.text("version") or cx.version_id())}


@route("POST", "/version", "手动另存一版（可把父版本标注一起带过去）")
def _new_version(cx: Ctx):
    return cx.hub.new_version(cx.p.text("dataset") or cx.dataset_id(),
                              parent_id=cx.p.text("parent"), op=cx.p.text("op", "manual"),
                              params=cx.p.dict_("params"), carry=cx.p.flag("carry", True))


# ---------------------------------------------------------------------- 智能标注

def _smart_params(cx: Ctx) -> dict:
    """后端名 + 后端参数 + 提示 + 这一层别的开关（workers/budget/k/rounds…）。

    除了路由自己要用的键，其余参数一律原样收下：界面上多一个滑块不必改路由，
    名字对上 services 的取法就生效——否则前端每加一个开关都要来回改三处。
    """
    p = cx.p
    params = dict(p.dict_("params"))
    params.update(p.rest())
    for key in ("workers", "budget", "rounds", "k", "limit", "threshold", "min_f1",
                "seed", "jitter_px"):
        if key in params:
            value = p.float_(key)
            params[key] = int(value) if key in ("workers", "budget", "rounds", "k",
                                                "limit", "seed") else value
    if "coords" in params:
        params["coords"] = [float(c) for c in p.list_("coords")]
    if "backend_params" in params:
        params["backend_params"] = p.dict_("backend_params")
    return params


@route("POST", "/smart/same_image", "同图找相似：给一个框，找出这一张里所有同类")
def _same_image(cx: Ctx):
    return cx.hub.same_image(cx.p.need("uri"), _smart_params(cx))


@route("POST", "/smart/preannotate", "整批预标注（干跑，不落库）")
def _preannotate(cx: Ctx):
    return cx.hub.preannotate(cx.p.list_("uris"), _smart_params(cx))


@route("POST", "/smart/apply", "整批预标注并落新版本：人工成果一起带过去")
def _smart_apply(cx: Ctx):
    return cx.hub.preannotate(cx.p.list_("uris"), _smart_params(cx),
                              apply_to=cx.p.text("version") or cx.version_id())


@route("POST", "/smart/undo", "撤销机器结果 = head 指回父版本，不删任何东西")
def _smart_undo(cx: Ctx):
    return cx.hub.undo_machine(cx.p.need("version"))


@route("GET", "/smart/speedup", "并行加速比（论文实验数据）")
def _speedup(cx: Ctx):
    return cx.hub.speedup(cx.p.list_("uris"), _smart_params(cx))


@route("GET", "/smart/hard", "难例排序：机器最没把握的先给人看")
def _hard(cx: Ctx):
    return cx.hub.hard_examples(cx.p.text("version") or cx.version_id(),
                               cx.p.list_("uris"), _smart_params(cx))


@route("POST", "/smart/active_round", "主动学习一轮：选样 + 模拟标注 + 落库")
def _active_round(cx: Ctx):
    return cx.hub.active_round(cx.p.text("version") or cx.version_id(),
                               cx.p.list_("uris"), _smart_params(cx))


@route("POST", "/smart/active_run", "主动学习循环（带 F1 门槛的准入判定）")
def _active_run(cx: Ctx):
    return cx.hub.active_run(cx.p.text("version") or cx.version_id(),
                             cx.p.list_("uris"), _smart_params(cx))


@route("GET", "/smart/history", "每一轮主动学习的记录")
def _active_history(cx: Ctx):
    return {"rounds": cx.hub.round_history(cx.p.text("version") or cx.version_id())}


# ---------------------------------------------------------------------- 清洗

@route("GET", "/clean/preview", "预览：每个算子会挑出哪些条目（与执行同一条代码路径）")
def _clean_preview(cx: Ctx):
    return cx.hub.clean_preview(cx.p.list_("uris"), cx.p.list_("specs"),
                               calibration=cx.p.text("calibration"))


@route("POST", "/clean/preview", "预览（条目多时用 POST，避免查询串过长）")
def _clean_preview_post(cx: Ctx):
    return cx.hub.clean_preview(cx.p.list_("uris"), cx.p.list_("specs"),
                               calibration=cx.p.text("calibration"))


@route("POST", "/clean/apply", "执行清洗：产新版本，原件一条不动")
def _clean_apply(cx: Ctx):
    return cx.hub.clean_apply(cx.p.text("version") or cx.version_id(), cx.p.list_("specs"),
                              uris=cx.p.list_("uris"), calibration=cx.p.text("calibration"),
                              write_transformed=cx.p.flag("write_transformed"),
                              author=cx.p.text("author"))


@route("POST", "/clean/rollback", "一键回退：只删登记在册且字节对得上的产物")
def _clean_rollback(cx: Ctx):
    return cx.hub.clean_rollback(cx.p.need("version"))


@route("GET", "/clean/dropped", "这个清洗版本丢掉了哪些条目")
def _clean_dropped(cx: Ctx):
    return {"dropped": cx.hub.clean_dropped(cx.p.text("version") or cx.version_id())}


# ---------------------------------------------------------------------- 增强

@route("GET", "/augment/recipes", "分学科配方与算子表（界面上「一键增强」的数据来源）")
def _augment_presets(cx: Ctx):
    from ..augment.policy import TASK_PRESETS

    ops, presets = cx.hub.operators("augment")
    return {"ops": ops, "presets": presets, "task_presets": TASK_PRESETS}


@route("POST", "/augment/suggest", "按数据集内容与模态推荐配方")
def _augment_suggest(cx: Ctx):
    from ..augment.policy import suggest

    preset = suggest(cx.hub.dataset(cx.dataset_id()), _dataset_items(cx))
    return {"preset": preset, "recipe": preset.get("recipe") or {},
            "reason": preset.get("reason", "")}


@route("POST", "/augment/check", "执行前的告警：这类数据这么增强会出问题")
def _augment_check(cx: Ctx):
    return cx.hub.augment_check(cx.p.list_("uris"), cx.p.dict_("recipe"),
                               category=cx.p.text("category"))


@route("POST", "/augment/run", "执行增强：新版本 + 谱系 + 落盘文件")
def _augment_run(cx: Ctx):
    return cx.hub.augment_run(cx.p.text("version") or cx.version_id(),
                              cx.p.dict_("recipe"), uris=cx.p.list_("uris"),
                              author=cx.p.text("author"))


@route("GET", "/augment/lineage", "逐条增强谱系：这条从哪个原件、按什么参数链来的")
def _augment_lineage(cx: Ctx):
    return {"lineage": cx.hub.augment_lineage(cx.p.text("version") or cx.version_id())}


@route("POST", "/augment/replay", "按版本里的 seed 重放一遍参数，校验可复现")
def _augment_replay(cx: Ctx):
    return cx.hub.augment_replay(cx.p.need("version"))


def _dataset_items(cx: Ctx) -> list:
    """当前版本条目的 DataItem 列表（policy.suggest 要看模态分布）。"""
    from .. import io as MDIO

    rows = cx.hub.items(cx.dataset_id(), cx.version_id())
    out = []
    for row in rows:
        try:
            out.append(MDIO.load_any(row["uri"]))
        except Exception:       # noqa: BLE001 - 推荐用不着为一坏路文件报错
            continue
    return out


# ---------------------------------------------------------------------- 导出

@route("GET", "/export/formats", "导出格式与其可调参数")
def _export_formats(cx: Ctx):
    return {"formats": cx.hub.operators("export"), "out_dir": str(cx.hub.out_dir / "export")}


@route("POST", "/export", "导出数据集（可选顺带生成训练脚本与说明）")
def _export(cx: Ctx):
    return cx.hub.export(cx.p.text("version") or cx.version_id(), cx.p.need("format"),
                         params=cx.p.dict_("params"), out_dir=cx.p.text("out_dir"),
                         make_scripts=cx.p.flag("scripts"))


@route("POST", "/export/validate", "回读校验导出目录与版本是否一致")
def _export_validate(cx: Ctx):
    return cx.hub.export_validate(cx.p.text("version") or cx.version_id(),
                                  cx.p.need("dir"))


@route("GET", "/export/report", "这个版本导出过哪些次（任务与报告）")
def _export_report(cx: Ctx):
    return {"reports": cx.hub.export_report(cx.p.text("version") or cx.version_id())}


# ---------------------------------------------------------------------- 收录（文件搬运）

@route("POST", "/collect", "把选中的条目收进数据集目录，写撤销清单")
def _collect(cx: Ctx):
    return cx.hub.collect(cx.p.list_("uris"), cx.p.need("target"),
                          mode=cx.p.text("mode", "copy"), flatten=cx.p.flag("flatten"))


@route("POST", "/collect/undo", "按清单回退收录（目标文件被改过就拒绝，除非 force）")
def _collect_undo(cx: Ctx):
    return cx.hub.collect_undo(cx.p.need("manifest"), keep_dirs=cx.p.flag("keep_dirs", True),
                               force=cx.p.flag("force"))


@route("GET", "/collect/latest", "目标目录里最近一份收录清单")
def _collect_latest(cx: Ctx):
    return cx.hub.collect_latest(cx.p.need("target"))


# ---------------------------------------------------------------------- 任务

@route("GET", "/tasks", "后台任务列表（清洗/增强/导出都留痕）")
def _tasks(cx: Ctx):
    rows = cx.hub.tasks(kind=cx.p.text("kind"), dataset_id=cx.p.text("dataset"))
    return {"tasks": rows, "n": len(rows)}


# ---------------------------------------------------------------------- 分发

def paths() -> list[dict]:
    """路由索引，`GET /api` 直接吐给前端，也当接口文档用。"""
    ordered = sorted(ROUTES.values(), key=lambda r: (r.path, r.method))
    return [{"method": r.method, "path": API_PREFIX + r.path, "doc": r.doc}
            for r in ordered]


def status_for(exc: Exception) -> int:
    """异常 -> HTTP 状态码：越权 403、找不到 404、参数不对 400，其余 500。"""
    from ..annotate.state import PermissionDenied

    if isinstance(exc, Bad):
        return 400
    if isinstance(exc, (PermissionDenied, PermissionError)):
        return 403
    if isinstance(exc, (Missing, KeyError, FileNotFoundError)):
        return 404
    if isinstance(exc, ValueError):
        return 400
    return 500


def call(workspaces: Workspaces, method: str, path: str,
         query: Optional[dict] = None, body: Optional[dict] = None) -> Response:
    """执行一次 API 调用（不建 socket）：`server.py` 和测试都走这里。"""
    method = (method or "GET").upper()
    clean = (path or "").rstrip("/") or API_PREFIX
    if clean == API_PREFIX:
        return Response.of({"endpoints": paths(), "workspaces": workspaces.names()})
    if not clean.startswith(API_PREFIX + "/"):
        return Response.of({"error": f"未知路径 {path}"}, 404)

    p = Params(query, body)
    try:
        hub = workspaces.hub(p.text("ws"))
    except UnknownWorkspace as exc:
        return Response.of({"error": str(exc.args[0]) if exc.args else "未知工作区"}, 404)

    key = (method, clean[len(API_PREFIX):])
    found = ROUTES.get(key)
    if found is None:
        allowed = [m for (m, pt) in ROUTES if pt == key[1]]
        if allowed:
            return Response.of({"error": f"{key[1]} 只支持 {'/'.join(sorted(set(allowed)))}，"
                                        f"收到 {method}"}, 405)
        return Response.of({"error": f"未知接口 {key[1]}"}, 404)
    try:
        return Response.of(found.handler(Ctx(workspaces, hub, p, p.text("ws"))))
    except Exception as exc:      # noqa: BLE001 - 接口不能把异常原样抛给浏览器
        status = status_for(exc)
        payload = {"error": f"{type(exc).__name__}: {exc}" if status == 500 else str(exc),
                   "path": key[1], "method": method}
        return Response.of(payload, status)
