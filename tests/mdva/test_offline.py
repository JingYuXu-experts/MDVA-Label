"""离线纪律：这个平台不发起任何出站请求，数据不出机器。

两层保证，都要有：
1. 静态扫源码——任何"能连出去"的写法（requests/urllib.request/http.client/create_connection）
   出现在引擎里就是违规。`webbrowser` 是唯一例外：它只打开本机 127.0.0.1 的页面。
2. 动态拦截——把 socket 的连接与地址解析函数换成会抛错的桩，然后跑一遍完整流程
   （建数据集 → 出图 → 标注 → 预标注 → 清洗 → 增强 → 导出）。真连了外网就是这里炸。

顺带验明服务只绑回环：`--host 0.0.0.0` 在没有 `--allow-remote` 时必须拒绝启动。
"""

import ast
import socket
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest

from mdva import synth
from mdva.web.deps import Workspaces
from mdva.web.routes import call
from mdva.web.server import make_server

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "mdva"

#: 会主动对外连接的模块名（服务端 listen/accept 不在此列）
FORBIDDEN = ("requests", "httpx", "urllib3", "aiohttp", "ftplib", "smtplib",
             "urllib.request", "urllib.error", "http.client", "socketio", "websockets")
ALLOWED_MODULES = {"socket", "webbrowser"}      # 只做本地用，逐条在下面核对


def _imports(path: Path) -> set:
    """文件里所有 import 的顶层与二级模块名（走 AST，不受注释与字符串干扰）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
                out.add(".".join(alias.name.split(".")[:2]))
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
            out.add(".".join(node.module.split(".")[:2]))
    return out


def test_no_engine_module_imports_an_http_client():
    offenders = {}
    for path in SRC_ROOT.rglob("*.py"):
        hit = _imports(path) & set(FORBIDDEN)
        if hit:
            offenders[str(path.relative_to(SRC_ROOT))] = sorted(hit)
    assert offenders == {}


def test_only_the_http_adapter_touches_network_apis():
    """允许碰网络模块的只有标准库 HTTP 适配器：别的地方出现 socket/webbrowser 要能说清理由。"""
    users = {p.relative_to(SRC_ROOT).as_posix() for p in SRC_ROOT.rglob("*.py")
             if _imports(p) & ALLOWED_MODULES}
    assert users == {"web/server.py"}


def test_webbrowser_is_used_only_for_the_local_url():
    text = (SRC_ROOT / "web" / "server.py").read_text(encoding="utf-8")
    assert "webbrowser.open(url)" in text
    assert "http://" in text and "https://" not in text       # 只可能打开本机 http 页面


# ---------------------------------------------------------------------- 动态拦截

class _Blocked(RuntimeError):
    pass


@pytest.fixture
def no_outbound_network(monkeypatch):
    """把"连出去"这条路焊死：连本机回环仍然允许，其余一律抛错。"""
    calls = []

    def guard(action, address):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and (host.startswith("127.") or host in ("localhost", "::1")):
            return
        calls.append((action, address))
        raise _Blocked(f"离线自检：拦截了对 {address!r} 的 {action}")

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo
    real_create = socket.create_connection

    def connect(self, address, *args, **kwargs):
        guard("connect", address)
        return real_connect(self, address, *args, **kwargs)

    def connect_ex(self, address, *args, **kwargs):
        guard("connect_ex", address)
        return real_connect_ex(self, address, *args, **kwargs)

    def getaddrinfo(host, *args, **kwargs):
        # 解析域名本身就是出站 DNS，离线环境里连不上也绝不该试
        guard("getaddrinfo", host)
        return real_getaddrinfo(host, *args, **kwargs)

    def create_connection(address, *args, **kwargs):
        guard("create_connection", address)
        return real_create(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    yield calls
    assert calls == [], f"跑流程时试图连出去：{calls}"


# ---------------------------------------------------------------------- 全流程

@pytest.fixture(scope="module")
def offline_ws(tmp_path_factory):
    pytest.importorskip("astropy")
    root = tmp_path_factory.mktemp("offline")
    data = synth.make_dataset(root / "aerospace", n_telemetry=900, seed=11)
    ws = Workspaces(root)
    yield ws, data, root
    ws.close()


def test_full_workflow_never_opens_a_connection(offline_ws, no_outbound_network):
    """建库 → 出图 → 标注 → 预标注 → 清洗 → 增强 → 导出，全程不许碰网络。"""
    ws, data, root = offline_ws
    created = call(ws, "POST", "/api/datasets", None,
                   {"name": "离线自检", "type": "detection", "root": str(data),
                    "category": "aerospace", "labels": ["耀斑区"]})
    assert created.status == 200, created.json_body
    did, vid = created.json_body["dataset_id"], created.json_body["version_id"]
    scene = str(data / "image_field.fits")
    series = str(data / "telemetry_sim.csv")
    at = {"dataset": did}

    assert call(ws, "GET", "/api/frame", {**at, "uri": scene, "width": 128}).status == 200
    assert call(ws, "GET", "/api/items", at).status == 200
    assert call(ws, "POST", "/api/annotations", None,
                {**at, "version": vid, "uri": scene,
                 "anns": [{"geom": "box", "coords": [12, 12, 60, 60], "label": "耀斑区"}],
                 "role": "annotator"}).status == 200

    smart = call(ws, "POST", "/api/smart/apply", None,
                 {**at, "version": vid, "uris": [scene], "backend": "cosmic_reject",
                  "backend_params": {"nsigma": 3.0}})
    assert smart.status == 200, smart.json_body
    assert smart.json_body["n_written"] >= 1

    clean = call(ws, "POST", "/api/clean/apply", None,
                 {**at, "version": vid, "uris": [scene, series],
                  "specs": [{"name": "blur"}]})
    assert clean.status == 200, clean.json_body

    aug = call(ws, "POST", "/api/augment/run", None,
               {**at, "version": vid, "uris": [scene], "recipe": {"seed": 1, "factor": 1,
                                                                  "ops": [{"name": "flip",
                                                                           "params": {
                                                                           "p_horizontal":
                                                                               1.0}}]}})
    assert aug.status == 200, aug.json_body

    exported = call(ws, "POST", "/api/export", None,
                    {**at, "version": vid, "format": "coco", "params": {"seed": 1}})
    assert exported.status == 200, exported.json_body
    check = call(ws, "POST", "/api/export/validate", None,
                 {**at, "version": vid, "dir": exported.json_body["out_dir"]})
    assert check.json_body["ok"] is True, check.json_body["summary"]
    assert Path(exported.json_body["out_dir"]).is_dir()
    # 时序也走一遍：加载与渲染都只读本地文件
    assert call(ws, "GET", "/api/frame", {**at, "uri": series, "width": 320}).status == 200


def test_serving_locally_is_allowed_and_public_bind_is_refused(offline_ws):
    """起服务、本机访问都合法；绑到对外地址必须被拒绝。"""
    ws, data, root = offline_ws
    httpd = make_server(ws, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        conn = HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
        conn.request("GET", "/api/health")
        resp = conn.getresponse()
        assert resp.status == 200
        resp.read()
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    with pytest.raises(ValueError):
        make_server(ws, "0.0.0.0", 0)


def test_generated_training_scripts_contain_no_urls(offline_ws, tmp_path):
    """导出的训练脚本必须能离线跑：里面不该有下载数据集/权重的 URL。"""
    ws, data, root = offline_ws
    created = call(ws, "POST", "/api/datasets", None,
                   {"name": "脚本检查", "type": "detection", "root": str(data),
                    "labels": ["耀斑区"]}).json_body
    call(ws, "POST", "/api/annotations", None,
         {"version": created["version_id"], "uri": str(data / "image_field.fits"),
          "anns": [{"geom": "box", "coords": [8, 8, 48, 48], "label": "耀斑区"}],
          "role": "annotator"})
    report = call(ws, "POST", "/api/export", None, {"version": created["version_id"],
                                                    "format": "yolo",
                                                    "scripts": True}).json_body
    out = Path(report["out_dir"])
    for name in ("train.py", "DATASET.md"):
        text = (out / name).read_text(encoding="utf-8")
        assert "http://" not in text and "https://" not in text, name


def test_probe_is_fully_local(offline_ws, no_outbound_network):
    """能力探测读的是本机信息（CPU/内存/显卡），不该联网查版本。"""
    body = call(offline_ws[0], "GET", "/api/probe").json_body
    assert body["cpu_count"] >= 1 and body["tier"] in ("high", "mid", "low")
