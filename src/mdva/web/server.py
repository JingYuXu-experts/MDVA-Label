"""标准库 HTTP 适配器：把 `routes.call()` 接到 socket 上，再托管前端构建产物。

为什么不用 FastAPI：这套东西要能在不装任何额外包的情况下 `python -m mdva serve`
就跑起来（用户的 conda 环境由他自己决定装什么）。路由与传输已经分层了，
将来要换 ASGI 只动这个文件。

三条硬约束：
- 只绑 127.0.0.1（`--allow-remote` 才放开，且必须显式给）；数据不出机器。
- 不发起任何出站请求：本模块只 import 标准库 + 本项目。
- 静态文件只在 `static/` 目录内解析，路径穿越一律拒绝。
"""

from __future__ import annotations

import json
import mimetypes
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, unquote, urlparse

from . import routes
from .deps import Workspaces

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 8 * 1024 * 1024        # 8 MB：界面提交的是一屏标注，超过就是误用
API_CACHE = "no-store"            # 接口必须即时：刚标的东西刷一下看不见会被判"假项目"
FRAME_CACHE = "private, max-age=30"
ASSET_CACHE = "public, max-age=31536000, immutable"

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("application/wasm", ".wasm")


def is_loopback(host: str) -> bool:
    """回环判定：`localhost` 与 127.x 之外一律算对外，需要显式授权。"""
    if host in ("localhost", "::1", ""):
        return True
    try:
        return host == socket.gethostbyname(socket.gethostname()) or host.startswith("127.")
    except OSError:
        return host.startswith("127.")


class _MissingPage(Exception):
    """静态目录不存在（前端还没构建）。"""


class _BodyTooLarge(ValueError):
    """请求体超过 MAX_BODY → 413，与"JSON 语法错"（400）分开报，前端提示才不误导。"""


def _static_file(static_dir: Path, url_path: str) -> Path:
    """URL 路径 -> static 目录内的真实文件；越界或不存在都抛 `_MissingPage`。"""
    rel = unquote(url_path).lstrip("/") or "index.html"
    candidate = (static_dir / rel).resolve()
    root = static_dir.resolve()
    if root not in candidate.parents and candidate != root:
        raise _MissingPage(f"越界路径 {url_path}")
    if candidate.is_dir():
        candidate = candidate / "index.html"
    if not candidate.is_file():
        raise _MissingPage(f"没有 {url_path}")
    return candidate


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "mdva"
    protocol_version = "HTTP/1.1"          # 复用连接：拖动时间轴时不必每次重建 TCP
    workspaces: Workspaces = None          # 由 make_server 注入
    static_dir: Optional[Path] = None
    verbose = False

    # ------------------------------------------------------------------ 入口

    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("GET", head_only=True)

    def do_POST(self) -> None:
        self._handle("POST")

    # DEV-01（契约核对 v1.2）：PUT/DELETE/PATCH 若不定义，stdlib 会回 501+HTML；
    # 契约 §1.3/§3 要求其余方法统一 405 {error} JSON，走 _handle 即可命中该分支。
    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_OPTIONS(self) -> None:
        # 只服务本机界面：允许任何来源，等于给"从别的端口开的前端页面"留条路
        self._send(204, {"Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                         "Access-Control-Allow-Headers": "Content-Type"}, b"")

    def _handle(self, method: str, head_only: bool = False) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith(routes.API_PREFIX):
            self._api(method, parsed, head_only)
        elif method in ("GET", "HEAD"):
            self._static(parsed.path, head_only)
        else:
            # 静态目录只读；写操作一律走 /api，不然 POST /index.html 会悄悄返回 200
            self._json(405, {"error": f"{parsed.path} 不是接口路径，写操作请用 /api/…"})

    # ------------------------------------------------------------------ API

    def _api(self, method: str, parsed, head_only: bool) -> None:
        try:
            body = self._json_body()
        except _BodyTooLarge as exc:
            self._json(413, {"error": str(exc)})
            return
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        if method not in ("GET", "POST"):
            self._json(405, {"error": f"{parsed.path} 只支持 GET/POST"})
            return
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        response = routes.call(self.workspaces, method, parsed.path, query, body)
        heads = {"Cache-Control": API_CACHE,
                 "Access-Control-Allow-Origin": self.headers.get("Origin") or "*",
                 "Content-Type": response.content_type, **response.headers}
        if response.data is not None:
            heads["Cache-Control"] = FRAME_CACHE if parsed.path.endswith("/frame") \
                or parsed.path.endswith("/mosaic") else API_CACHE
            self._send(response.status, heads, response.data, head_only)
        else:
            self._send(response.status, heads, _dump(response.json_body), head_only)

    def _json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise _BodyTooLarge(f"请求体过大（{length} > {MAX_BODY}）：一次提交一屏标注就够")
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ValueError(f"请求体必须是 UTF-8 编码的 JSON：{exc}") from None
        except json.JSONDecodeError as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}") from None
        if not isinstance(parsed, dict):
            raise ValueError("请求体要是一个 JSON 对象")
        return parsed

    # ------------------------------------------------------------------ 静态

    def _static(self, url_path: str, head_only: bool) -> None:
        static_dir = self.static_dir
        if static_dir is None or not static_dir.is_dir():
            self._send(200, {"Content-Type": "text/html; charset=utf-8",
                             "Cache-Control": API_CACHE},
                       _build_hint(str(static_dir)).encode("utf-8"))
            return
        try:
            path = _static_file(static_dir, url_path)
            data = path.read_bytes()
        except _MissingPage:
            # 单页应用：未知路径交给 index.html，前端路由自己决定显示哪一页
            try:
                path, data = (static_dir / "index.html"), (static_dir / "index.html").read_bytes()
            except OSError:
                self._send(404, {"Content-Type": "text/plain; charset=utf-8"},
                           b"frontend not built")
                return
        ext = path.suffix.lower()
        heads = {"Content-Type": mimetypes.guess_type(str(path))[0]
                 or "application/octet-stream",
                 "Cache-Control": ASSET_CACHE if _is_hashed(path.name) else "no-cache"}
        if ext == ".html":
            heads["Cache-Control"] = API_CACHE      # index 变了一律立刻重取，否则用户看的是旧界面
        self._send(200, heads, data, head_only)

    # ------------------------------------------------------------------ 发送

    def _json(self, status: int, payload) -> None:
        self._send(status, {"Content-Type": "application/json; charset=utf-8",
                            "Cache-Control": API_CACHE}, _dump(payload))

    def _send(self, status: int, headers: dict, body: bytes, head_only: bool = False) -> None:
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        if status != 204:          # 204 按规范不能有正文，连长度都不报
            self.send_header("Content-Length", str(len(body or b"")))
        self.end_headers()
        # HEAD 报的是"GET 会给多少字节"，所以正文按 head_only 决定写不写
        if body and status != 204 and not head_only:
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        if self.verbose:
            super().log_message(fmt, *args)


def _is_hashed(name: str) -> bool:
    """Vite 产物名里带内容哈希（app.4f2c9a.js），可以放心长期缓存。"""
    stem = Path(name).stem
    return "-" in stem and any(ch.isdigit() for ch in stem.split("-")[-1])


def _dump(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


def _build_hint(static_dir: str) -> str:
    """前端还没构建时给的一句话，别让用户对着空白页猜。"""
    return (
        "<!doctype html><meta charset=utf-8><title>mdva 服务已启动</title>"
        "<style>body{background:#14161A;color:#DDE2EA;font:14px/1.7 system-ui,"
        "-apple-system,Segoe UI,sans-serif;margin:40px max(24px,12vw)}"
        "code{background:#1B1E24;padding:2px 6px;border-radius:4px;color:#8FB8FF}"
        "h1{font-size:18px;font-weight:600}</style>"
        "<h1>后端已经在跑了，前端还没构建</h1>"
        "<p>接口现在就能用：<a style=\"color:#8FB8FF\" href=\"/api\">/api</a>"
        "（列出全部端点）。</p>"
        "<p>要界面请在项目根目录执行：</p>"
        f"<p><code>cd frontend && npm install && npm run build</code></p>"
        f"<p>产物会落到 <code>{static_dir}</code>，刷新这一页即可。</p>"
        "<p>只想改前端边看边调：<code>npm run dev</code>，它会把 /api 代理到本服务。</p>")


def make_server(workspaces, host: str = "127.0.0.1", port: int = 8765,
                static_dir=None, verbose: bool = False,
                allow_remote: bool = False) -> ThreadingHTTPServer:
    """建服务但不监听：测试要 bind 到 0 端口再自己起线程。"""
    ws = workspaces if isinstance(workspaces, Workspaces) else Workspaces(workspaces)
    handler = type("BoundHandler", (ApiHandler,), {})   # 每个服务一套类属性，互不串台
    handler.workspaces = ws
    handler.static_dir = Path(static_dir) if static_dir else STATIC_DIR
    handler.verbose = verbose
    if not is_loopback(host) and not allow_remote:
        # 数据集可能是别人的科研数据，默认绝不监听对外地址
        raise ValueError(f"拒绝在非回环地址 {host} 上监听；确实要放开请带 --allow-remote")
    httpd = ThreadingHTTPServer((host, int(port)), handler)
    httpd.daemon_threads = True
    httpd.mdva_workspaces = ws
    return httpd


def serve(workspaces, host: str = "127.0.0.1", port: int = 8765, static_dir=None,
          verbose: bool = False, open_browser: bool = False,
          allow_remote: bool = False) -> int:
    """阻塞式启动（命令行用）。返回进程退出码。"""
    if not is_loopback(host) and not allow_remote:
        print(f"注意：{host} 不是回环地址，整个数据集会暴露在这个网卡上。"
              f"确认要这样请加 --allow-remote")
        return 2
    try:
        httpd = make_server(workspaces, host, port, static_dir, verbose, allow_remote)
    except OSError as exc:
        print(f"起不来：{exc}（端口 {port} 大概被占了，加 --port 换一个）")
        return 1
    shown = f"http://{host}:{httpd.server_address[1]}"
    print(f"mdva 标注平台 -> {shown}/  （API 索引 {shown}/api）")
    print(f"工作区根目录 {httpd.mdva_workspaces.root}；Ctrl+C 退出")
    if open_browser:
        threading.Timer(0.4, _open, args=(shown,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
        httpd.mdva_workspaces.close()
    return 0


def _open(url: str) -> None:
    """只调用系统浏览器打开本地页面，不产生任何对外网络请求。"""
    import webbrowser

    try:
        webbrowser.open(url)
    except Exception:      # noqa: BLE001 - 没浏览器/无桌面会话时别拖累服务
        pass
