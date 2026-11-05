"""L6 服务层：把引擎能力暴露成一个只监听本机的 HTTP 接口 + 前端静态页。

分四块，谁都不越界：
- `services.Hub`：一次调用 = 一个界面动作，返回 JSON 安全的 dict（纯 Python，不依赖框架）。
- `deps.Workspaces`：目录 → 工作区名 → Hub 的注册表；请求只认名字，不认路径。
- `routes`：路由表 + 参数解析 + 状态码映射，不建 socket 也能整表测。
- `server`：标准库 `ThreadingHTTPServer` 适配器，顺带托管 `static/` 里的前端产物。

本层不 import `app.py`（那个 tkinter 查看器是独立入口），也不发起任何出站请求。
"""

from .deps import Workspaces, discover
from .routes import Response, call, paths
from .server import make_server, serve
from .services import Hub

__all__ = ["Hub", "Response", "Workspaces", "call", "discover", "make_server", "paths",
           "serve"]
