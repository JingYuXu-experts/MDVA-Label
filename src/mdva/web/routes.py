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
import re
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

    def rewrite(self, key: str, value) -> None:
        """把收口归一后的值写回原处（请求体优先，与 `raw()` 的取用顺序一致）。"""
        if key in self._b:
            self._b[key] = value
        else:
            self._q[key] = value

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

    def inbound(self) -> dict:
        """全部入站键的合并视图（请求体优先），**含**路由键——给递归路径尺用。

        与 `rest()` 的差别就是存在的理由：`rest()` 按 `ROUTING_KEYS` 挡键，而路径值可以
        藏在 `params` 整包嵌套里；`guard_paths` 要看的是"这次请求送进来的每一个值"。
        """
        merged = dict(self._q)
        merged.update(self._b)
        return merged

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
            'revision': 'web-solar-ai-4', 'sources': list(cx.hub.store.source_roots),
            "workspaces": cx.ws.names(), "default": cx.ws.default_ws(),
            "out_dir": str(cx.hub.out_dir), "offline": __import__("os").environ.get("MDVA_MONITOR_ONLINE") != "1", "monitor_revision": "sw-2"}


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
    if cx.p.has('page'):
        return cx.hub.items_page(cx.dataset_id(), cx.version_id(),
                                 page=cx.p.int_('page', 1), page_size=cx.p.int_('page_size', 120),
                                 state=cx.p.text('state'), modality=cx.p.text('modality'),
                                 search=cx.p.text('search'), category=cx.p.text('category'))
    rows = cx.hub.items(cx.dataset_id(), cx.version_id(), state=cx.p.text("state"),
                        modality=cx.p.text("modality"))
    return {"items": rows, "n": len(rows)}


@route('GET', '/sources', '已登记的只读数据源（仅返回 ID）')
def _sources(cx: Ctx):
    from .solar import Solar
    return {'sources': Solar(cx.hub).sources()}


@route('GET', '/items/uris', '当前版本的全部条目 URI，用于用户显式选择全量处理')
def _item_uris(cx: Ctx):
    version = cx.version_id()
    dataset = cx.hub.store.get_dataset(cx.dataset_id()) or {}
    if (dataset.get('meta') or {}).get('indexed'):
        uris = [r['uri'] for r in cx.hub.store.version_records(version)]
    else:
        uris = [r['uri'] for r in cx.hub.items(cx.dataset_id(), version)]
    return {'uris': uris, 'n': len(uris)}


@route('POST', '/imports/preview', '只读预检 SSAFD：配对、尺寸与 NOAA 对应关系')
def _import_preview(cx: Ctx):
    from .solar import Solar
    return Solar(cx.hub).preflight(cx.p.need('source_id'))


@route('POST', '/imports', '按预检记录建立全量或固定种子小样本索引')
def _import_execute(cx: Ctx):
    from .solar import Solar
    size = cx.p.int_('sample_per_category', 0)
    if not 0 <= size <= 10000:
        raise Bad('sample_per_category 必须在 0–10000 之间，0 表示全量')
    return Solar(cx.hub).execute(cx.p.need('scan_id'), cx.p.text('name'), size, cx.p.int_('seed', 20261107))


@route('GET', '/reference', '原始掩膜的只读实例清单')
def _reference(cx: Ctx):
    from .solar import Solar
    return Solar(cx.hub).reference(cx.p.need('uri'))


@route('GET', '/solar/catalog', '完整太阳数据产品登记状态')
def _solar_catalog(cx: Ctx):
    from .solar_catalog import SolarCatalog
    return SolarCatalog(cx.hub).status()


@route('POST', '/solar/index', '建立观测、参数与耀斑事件的只读索引')
def _solar_index(cx: Ctx):
    from .solar_catalog import SolarCatalog
    return SolarCatalog(cx.hub).start_index()


@route('GET', '/solar/days', '观测日期分页')
def _solar_days(cx: Ctx):
    from .solar_catalog import SolarCatalog
    return SolarCatalog(cx.hub).days(cx.p.text('search'), cx.p.int_('page', 1))


@route('GET', '/solar/day', '同日多源观测、参数和事件')
def _solar_day(cx: Ctx):
    from .solar_catalog import SolarCatalog
    return SolarCatalog(cx.hub).day(cx.p.need('day'))


@route('GET', '/solar/region', '原始活动区与物理参数和事件标签的可核验关联')
def _solar_region(cx: Ctx):
    from .solar_catalog import SolarCatalog
    return SolarCatalog(cx.hub).region(cx.p.need('uri'), cx.p.int_('instance', 0))


@route('GET', '/solar/image', '已登记太阳图像的原始 JPG 预览')
def _solar_image(cx: Ctx):
    from .solar_catalog import SolarCatalog
    return Image(SolarCatalog(cx.hub).image(cx.p.need('uri')), content_type='image/jpeg')


@route('GET', '/solar/baselines', '太阳模型论文和作者实现的核验状态，不冒充已复现模型')
def _solar_baselines(cx: Ctx):
    from .solar_catalog import BASELINES
    return {'baselines': BASELINES, 'selection_policy': '论文与作者实现核对 → 原条件复现 → 本地数据适配评测 → 决定接入；当前没有已通过复现验收的深度模型。'}


@route('GET','/solar/projects','太阳工作台项目列表')
def _projects(cx):
    from .solar_projects import projects
    return {'projects':projects(cx.hub)}


@route('POST','/solar/projects','创建托管图片项目')
def _new_project(cx):
    from .solar_projects import create
    return create(cx.hub,cx.p.need('name'))


@route('POST','/solar/projects/preferences','记录项目打开和收藏置顶')
def _project_preferences(cx):
    from .solar_projects import update_project
    return update_project(cx.hub,cx.p.need('project'),cx.p.need('action'),cx.p.flag('pinned'))


@route('GET','/solar/project','项目照片和批次')
def _project(cx):
    from .solar_projects import catalog
    return catalog(cx.hub,cx.p.need('project'))


@route('GET','/solar/project/sources','按文件名选择当前项目已有图片')
def _project_sources(cx):
    from .solar_projects import existing_images
    return existing_images(cx.hub,cx.p.need('project'),cx.p.text('search'),cx.p.int_('page',1),
                           refresh=cx.p.flag('refresh'),ids_only=cx.p.flag('ids_only'))


@route('GET','/solar/project/source-thumbnail','预览当前项目尚未导入的照片')
def _project_source_thumbnail(cx):
    from .solar_projects import source_thumbnail
    return Image(source_thumbnail(cx.hub,cx.p.need('project'),cx.p.need('id')),content_type='image/jpeg')


@route('POST','/solar/project/import-sources','批量载入当前项目已有照片')
def _project_import_sources(cx):
    from .solar_projects import import_sources
    return import_sources(cx.hub,cx.p.need('project'),cx.p.list_('ids'))


@route('POST','/solar/project/import','导入一张图片并返回缩略图编号')
def _project_import(cx):
    from .solar_projects import import_photo
    return import_photo(cx.hub,cx.p.need('project'),cx.p.inbound())


@route('GET','/solar/project/image','项目照片和缩略图')
def _project_image(cx):
    from .solar_projects import photo_asset
    raw,mime=photo_asset(cx.hub,cx.p.need('project'),cx.p.need('id'),cx.p.flag('thumb'))
    return Image(raw,content_type=mime)


@route('GET','/solar/project/estimate','按本机识别历史估算批次耗时')
def _project_estimate(cx):
    from .solar_projects import estimate
    return estimate(cx.hub,{key:cx.p.int_(key) for key in ('AR','CH') if cx.p.int_(key)>0},cx.p.text('device','auto'))


@route('POST','/solar/project/batch/precheck','核对目标、通道、照片输入与 UTC 观测范围')
def _project_batch_precheck(cx):
    from .solar_projects import precheck
    return precheck(cx.hub,cx.p.need('project'),cx.p.list_('selection'),cx.p.need('goal'),cx.p.dict_('window'))


@route('POST','/solar/project/batch','确认并登记项目批次')
def _project_batch(cx):
    from .solar_projects import submit
    return submit(cx.hub,cx.p.need('project'),cx.p.need('id'),cx.p.list_('selection'),cx.p.text('device','auto'),
                  target=cx.p.need('goal'),window=cx.p.dict_('window'))


@route('GET','/solar/project/batch','按选图顺序读取逐张进度及预测')
def _project_status(cx):
    from .solar_projects import status
    return status(cx.hub,cx.p.need('project'),cx.p.need('id'))


@route('POST','/solar/project/forecast/retry','仅重新匹配观测和计算事件试算')
def _project_forecast_retry(cx):
    from .solar_projects import retry_forecast
    return retry_forecast(cx.hub,cx.p.need('project'),cx.p.need('id'),cx.p.need('photo'))


@route('GET','/solar/project/download','下载批次或单张机器结果')
def _project_download(cx):
    from .solar_projects import archive
    return Image(archive(cx.hub,cx.p.need('project'),cx.p.need('id'),cx.p.text('photo')),content_type='application/zip')


@route('GET','/solar/project/regions','从完整掩膜读取区域边界和像素面积')
def _project_regions(cx):
    import io
    import numpy as np
    from PIL import Image as PILImage
    from .solar_products import regions
    from .solar_models import asset
    idx=cx.p.int_('index')
    raw,_=asset(cx.hub,cx.hub.out_dir,cx.p.need('id'),f'image-{idx:06d}/candidate-mask.png')
    with PILImage.open(io.BytesIO(raw)) as image:
        return {'regions':regions(np.array(image))}


@route('GET','/solar/models','本地AR/CH研究候选模型、输入条件及独立测试状态')
def _solar_models(cx: Ctx):
    from .solar_models import inventory,ensure_worker
    ensure_worker(cx.hub.out_dir)
    return inventory(cx.hub.out_dir)


@route('GET','/solar/review/evidence','读取本地历史太阳结构多图复核案例')
def _solar_review_evidence(cx: Ctx):
    from .solar_review import evidence
    return evidence(cx.hub.out_dir)


@route('GET','/solar/review/asset','按固定资源编号读取本地复核对照图')
def _solar_review_asset(cx: Ctx):
    from .solar_review import asset
    return Image(asset(cx.hub.out_dir,cx.p.need('id')))


@route('POST','/solar/analyze','上传一张观测图直接自动识别，无需建数据集或人工标注')
def _solar_analyze(cx: Ctx):
    from .solar_input import analyze
    return analyze(cx.hub,cx.hub.out_dir,cx.p.inbound())


@route('GET','/solar/inference/input','仅读取已登记任务的原始输入图像')
def _solar_input(cx: Ctx):
    from .solar_input import original
    raw,mime=original(cx.hub,cx.hub.out_dir,cx.p.need('id'),cx.p.int_('index',0))
    return Image(raw,content_type=mime)


@route('POST','/solar/inference','登记单图/批量候选任务；不写人工标注')
def _solar_inference(cx: Ctx):
    from .solar_models import create
    return create(cx.hub,cx.hub.out_dir,cx.p.inbound())


@route('GET','/solar/inference/jobs','当前工作区候选任务；断开浏览器不终止任务')
def _solar_inference_jobs(cx: Ctx):
    from .solar_models import jobs
    return jobs(cx.hub,cx.hub.out_dir,cx.p.text('id'))


@route('GET','/solar/inference/result','读取已完成逐图候选并核验原图身份')
def _solar_inference_result(cx: Ctx):
    from .solar_models import result
    return result(cx.hub,cx.hub.out_dir,cx.p.need('id'),cx.p.int_('index',0))


@route('GET','/solar/inference/assets','候选栅格或概率资源，只接受任务内资源ID')
def _solar_inference_assets(cx: Ctx):
    from .solar_models import asset
    raw,mime=asset(cx.hub,cx.hub.out_dir,cx.p.need('id'),cx.p.need('asset'))
    return Image(raw,content_type=mime)


@route('GET','/solar/inference/annotations','直接下载未经人工审核的自动标注与区域测量')
def _solar_inference_annotations(cx: Ctx):
    from .solar_models import annotations_archive
    return Image(annotations_archive(cx.hub,cx.hub.out_dir,cx.p.need('id')),
        content_type='application/zip')


@route('GET', '/reference/overlay', '保持原生几何的透明掩膜叠加图')
def _reference_overlay(cx: Ctx):
    from .solar import Solar
    return Image(Solar(cx.hub).overlay(cx.p.need('uri'), cx.p.int_('width', 960),
                                     cx.p.int_('height', 540), cx.p.int_('instance', 0)))


@route('GET', '/reference/polygons', '把原始实例轮廓近似为可编辑多边形，不改原掩膜')
def _reference_polygons(cx: Ctx):
    from .solar import Solar
    return Solar(cx.hub).polygons(cx.p.need('uri'), cx.p.int_('instance', 0))


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
    return cx.hub._rel(cx.hub.thumbnails(cx.p.list_("uris"), cell=cx.p.pair("cell"),
                               workers=cx.p.int_("workers", 0)))


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
                                  replace=cx.p.flag("replace", True),preserve_approved=cx.p.flag("preserve_approved"))


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


@route("POST", "/augment/preview", "当前增强配方的实际样例预览，不写入文件或版本")
def _augment_preview(cx: Ctx):
    return cx.hub.augment_preview(cx.p.list_("uris"),cx.p.dict_("recipe"),cx.p.text("uri"))


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
    if cx.p.text("mode", "copy") == "move":
        for uri in cx.p.list_("uris"):
            cx.hub.store.require_writable(uri)
    return cx.hub.collect(cx.p.list_("uris"), cx.p.need("target"),
                          mode=cx.p.text("mode", "copy"), flatten=cx.p.flag("flatten"))


@route("POST", "/collect/undo", "按清单回退收录（目标文件被改过就拒绝，除非 force）")
def _collect_undo(cx: Ctx):
    return cx.hub.collect_undo(cx.p.need("manifest"), keep_dirs=cx.p.flag("keep_dirs", True),
                               force=cx.p.flag("force"), enforce_boundary=True)


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


#: 请求里带磁盘路径的键——数据分析 seq=751 那张"键 × 收口点"表的落点版。
#: `uri/uris/target/dir/manifest/calibration` 本来就会走进 `store.to_abs`；
#: `root`（scan/bench/datasets）与 `out_dir`（export）**根本不进 to_abs**，
#: 所以收口必须做在这一层（请求边界），而不是往每个方法里补检查——PM seq=752 裁定 2：
#: 边界落在攻击者唯一进得来的那个面上，CLI/建包脚本（=启动参数的主人）不受影响。
PATH_KEYS = ("uri", "uris", "root", "target", "out_dir", "dir", "manifest", "calibration")

#: 会最终到达文件系统的路径值所在的键名（REQ-W27 覆盖面）。**判据不是这张表**——判据是
#: "值会不会到达文件系统"（PM seq=794 §一 裁定：键名清单降级为实现细节）。表里必须有
#: `source_uri` 这类**不在 `PATH_KEYS`、却经 `rest()` 与 `params` 两个门进来**的键：
#: `_smart_params` 把除 `ROUTING_KEYS` 外的入站键全收，`services.prompt_from` 再把它送进
#: `to_abs -> resolve_prompt`，`PATH_KEYS` 那把尺结构上看不见它。
#: **但名单本身不收敛**（判据附件 §2 门 5：`specs[].reference` 既不在 `PATH_KEYS`、也不在
#: 任何人列过的清单里，`clean/task.py:51-53` 把 `specs[]` 除 `name/op/params` 外的任意键
#: 并进算子参数）。所以本实现是两层：登记名一层（管得住门 1–5 这些**已经数出来的**名字），
#: **"其余全收"面上按路径形态再扫一层**（不看键名），后者管的是"下一枚没人登记过的名字"。
#: 漏登记会被 `tests/mdva/test_web_containment.py` 的两把尺当场抓红（行为面 a–f + 消费点静态尺）。
PATH_VALUE_KEYS = PATH_KEYS + ("source_uri", "src", "dst", "weights", "checkpoint",
                               "image_path", "reference", "template_uri")

#: 自由文本载体：`anns`/`ann` 里的 `label`/`note`、审核 `reason`、数据集 `labels` 这类**人写的
#: 文字**只按登记名校验、不按形态校验——否则评审在驳回理由里打一句 `见 C:\docs\spec` 就会把
#: 整个请求判死（那是"修过头"，W27-2 防的就是它）。名单以外的键（算法 knob、specs、params…）
#: 一律进形态层：往 knob 里塞绝对路径照样拒，这一条就是"下一枚没人登记过的名字"的背板。
FREE_TEXT_KEYS = ("anns", "ann", "labels", "reason", "note", "text", "comment", "message")

#: 路径形态判据：绝对形态（盘符 / UNC / 以 `/` 开头）或含 `..` 段。相对且无 `..` 的值不扫——
#: 那类值本来就按工作区 base 解析，天然落在允许根内。
_PATH_SHAPE = re.compile(r"(?:^[A-Za-z]:[\\/]|^\\\\|^/|(?:^|[/\\])\.\.(?:[/\\]|$))")


def _check_path_values(node, store, key=None, text_carrier=False) -> None:
    """递归校全部入站结构：登记名一律过尺；非自由文本的"其余全收"面上，路径形态的值也过尺。

    与层位无关，所以顶层 `?source_uri=`、请求体 `source_uri`、`params` 嵌套位、
    `specs[].prompts[].source_uri`、`specs[].reference` 走的是同一把尺——数据分析判据附件
    §4 的 a–e 五道门一次关掉，且新增字段名不需要再改这张表（形态层兜着）。
    """
    carrier = text_carrier or (key in FREE_TEXT_KEYS)
    if isinstance(node, dict):
        for sub_key, value in node.items():
            _check_path_values(value, store, str(sub_key), carrier)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _check_path_values(value, store, key, carrier)
    elif isinstance(node, str) and node.strip():
        text = node.strip()
        if not carrier and text.startswith(("[", "{")):
            try:
                decoded = json.loads(text)
            except (ValueError, RecursionError):
                raise Bad("容器参数不是合法 JSON") from None
            _check_path_values(decoded, store, key, carrier)
        elif key in PATH_VALUE_KEYS or (not carrier and _PATH_SHAPE.search(text)):
            store.require_within(text)


def _normalize_paths(node, store, key=None, carrier=False):
    carrier = carrier or key in FREE_TEXT_KEYS
    if isinstance(node, dict):
        return {k: _normalize_paths(v, store, k, carrier) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_normalize_paths(v, store, key, carrier) for v in node]
    if isinstance(node, str) and node.strip():
        text = node.strip()
        if not carrier and text.startswith(('[', '{')):
            return _normalize_paths(json.loads(text), store, key, carrier)
        if key in PATH_VALUE_KEYS or (not carrier and _PATH_SHAPE.search(text)):
            return str(store.require_within(text))
    return node


def guard_paths(p: "Params", hub) -> None:
    """在任何遍历 / open / 落盘之前，把路径参数校到允许根之内并归一（REQ-W27）。

    两件事一起做，缺一件都不算收口：
    1. **校**：越界 ⇒ `OutsideWorkspace`（路由层 400，原文点到被拒的那个值，不回显服务端路径）。
       它发生在 handler 之前，所以 `scan?root=C:\\Windows` 不会再走一遍系统盘（W27-6），
       也不会再冒出 `[Errno 13] Permission denied` + 系统路径那种答非所问（W27-7）。
    2. **归一**：相对的按工作区 base 算、绝对的解析干净，再交回 handler。
       只校不改的话，`.` 这类值仍会在 handler 里被解析成服务进程的 cwd（W27-3 追加项）——
       边界判对了、行为却走偏，是"键存在 ≠ 收口命中"的另一种形态。
    """
    store = hub.store
    for key in PATH_KEYS:
        if not p.has(key):
            continue
        value = p.raw(key)
        if key == "uris":
            value = p.list_(key)
        if isinstance(value, (list, tuple)):
            p.rewrite(key, [str(store.require_within(v)) for v in value if str(v or "").strip()])
        elif str(value or "").strip():
            p.rewrite(key, str(store.require_within(value)))
        if key in ("target", "out_dir") and str(value or "").strip():
            store.require_writable(p.raw(key))
    # 第二把尺：`rest()`/`params` 嵌套位上的路径值不在 PATH_KEYS 里（`source_uri` 就是这枚，
    # 判据附件 v1.0 §2 实测五道门全是 200 且真读）。判据按消费点，不按键名。
    _check_path_values(p.inbound(), store)
    for key, value in p.inbound().items():
        p.rewrite(key, _normalize_paths(value, store, key))


def status_for(exc: Exception) -> int:
    """异常 -> HTTP 状态码：越权 403、找不到 404、参数不对 400，其余 500。"""
    from ..annotate.state import PermissionDenied

    if isinstance(exc, Bad):
        return 400
    if isinstance(exc, RecursionError):
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

    if clean.startswith('/api/training/'):
        if method != 'GET':
            return Response.of({'error': '实验跟踪接口仅支持读取'}, 405)
        from .training import summary, detail, journal, files, artifact
        try:
            output = workspaces.out_dir or workspaces.root.parent/'out'
            training_params = Params(query)
            if clean.startswith('/api/training/diagnostics'):
                from . import diagnostics
                report_id = training_params.text('report')
                if clean == '/api/training/diagnostics':
                    return Response.of(diagnostics.summary(output, report_id))
                if clean == '/api/training/diagnostics/cases':
                    return Response.of(diagnostics.cases(output, report_id,
                        category=training_params.text('category', 'FL'), run_id=training_params.text('run_id'),
                        station=training_params.text('station'), error_type=training_params.text('error_type'),
                        page=int(training_params.text('page', '1')), page_size=int(training_params.text('page_size', '24')),
                        order=training_params.text('order', 'fixed'), subset=training_params.text('subset', 'validation'),
                        sample_id=training_params.text('sample_id')))
                if clean == '/api/training/diagnostics/assets':
                    raw, mime = diagnostics.asset(output, report_id, training_params.text('id'))
                    return Response(200, data=raw, content_type=mime, headers={'X-Content-Type-Options': 'nosniff'})
                return Response.of({'error': '未知诊断接口'}, 404)
            if clean == '/api/training/summary':
                return Response.of(summary(workspaces.root, output=output))
            if clean == '/api/training/run':
                return Response.of(detail(workspaces.root, training_params.text('id'), output=output))
            if clean == '/api/training/journal':
                return Response.of(journal(workspaces.root, int(training_params.text('page', '1')),
                    int(training_params.text('page_size', '12')), training_params.text('kind', 'all'), output=output))
            if clean == '/api/training/files':
                return Response.of(files(workspaces.root, output=output))
            if clean == '/api/training/file':
                if training_params.flag('download'):
                    from urllib.parse import quote
                    data, filename = artifact(workspaces.root, training_params.text('id'), output=output, download=True)
                    return Response(200, data=data, content_type='text/plain; charset=utf-8', headers={
                        'Content-Type': 'text/plain; charset=utf-8',
                        'Content-Disposition': "attachment; filename*=UTF-8''"+quote(filename),
                        'X-Content-Type-Options': 'nosniff'})
                return Response.of(artifact(workspaces.root, training_params.text('id'), output=output))
            return Response.of({'error': '未知实验接口'}, 404)
        except (ValueError, FileNotFoundError) as exc:
            return Response.of({'error': str(exc)}, 400 if isinstance(exc, ValueError) else 404)

    if clean.startswith('/api/sw/'):
        from .space_weather import dispatch
        return dispatch(workspaces, method, clean[len('/api/sw/'):], {**(query or {}), **(body or {})})
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
        guard_paths(p, hub)
        return Response.of(found.handler(Ctx(workspaces, hub, p, p.text("ws"))))
    except Exception as exc:      # noqa: BLE001 - 接口不能把异常原样抛给浏览器
        status = status_for(exc)
        payload = {"error": f"{type(exc).__name__}: {exc}" if status == 500 else str(exc),
                   "path": key[1], "method": method}
        return Response.of(payload, status)
