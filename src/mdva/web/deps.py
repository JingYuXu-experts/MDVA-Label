"""工作区注册表：一个目录（含 mdva.sqlite）= 一个工作区 = 一个 Hub。

界面只认工作区**名字**，不认路径：请求里出现的是 `ws=aerospace` 这样的键，
服务端从这张表里查。路径由启动参数一次性扫描确定，之后任何请求都没法
指到扫描结果之外的文件——否则一个只绑 127.0.0.1 的接口就成了读任意文件的口子。
"""

from __future__ import annotations

import threading
from pathlib import Path

from .services import Hub

DB_NAME = "mdva.sqlite"
MAX_DEPTH = 2          # 根目录本身 + 一级子目录（分学科数据集正好是这一层）


class UnknownWorkspace(KeyError):
    """请求里的工作区名字不在扫描结果里。"""


def discover(root) -> list[dict]:
    """找出 `root` 下可用作工作区的目录，返回 `[{ws, dir, db}]`，按名字排序。

    根目录**总是**一个工作区，哪怕还没有 mdva.sqlite：第一次启动时用户要能在界面上
    直接建第一个数据集，而不是被"没有数据库"挡在门外（库文件在 Hub 打开时才落盘）。
    子目录只收已经建好库的，免得把随便一个数据文件夹都当成工作区。
    """
    root = Path(root).resolve()
    found: dict[str, Path] = {}
    if root.is_dir():
        found["."] = root
        for depth in range(1, MAX_DEPTH):
            for child in sorted(root.glob("/".join(["*"] * depth))):
                if child.is_dir() and (child / DB_NAME).is_file():
                    found[child.relative_to(root).as_posix()] = child
    return [{"ws": name, "dir": str(path), "db": str(path / DB_NAME)}
            for name, path in sorted(found.items())]


class Workspaces:
    """Hub 的懒加载缓存：同一个 sqlite 文件只开一份连接与帧缓存。"""

    def __init__(self, root, out_dir=None):
        self.root = Path(root).resolve()
        self.out_dir = Path(out_dir).resolve() if out_dir else None
        self._hubs: dict[str, Hub] = {}
        self._lock = threading.Lock()
        self._table: dict[str, dict] = {}
        self.rescan()

    # ------------------------------------------------------------------ 发现

    def rescan(self) -> list[dict]:
        """重新扫描目录：用户在命令行里新建了数据集，界面点一下就能看见。"""
        rows = discover(self.root)
        with self._lock:
            self._table = {r["ws"]: r for r in rows}
            for name in list(self._hubs):
                if name not in self._table:            # 库文件被移走就关掉句柄
                    self._hubs.pop(name).close()
        return rows

    def list(self) -> list[dict]:
        out = []
        for name, row in sorted(self._table.items()):
            hub = self.hub(name)
            datasets = hub.datasets()
            out.append({"ws": name, "dir": row["dir"], "db": row["db"],
                        "n_datasets": len(datasets),
                        "datasets": [{"id": d["id"], "name": d["name"], "type": d["type"],
                                      "head": d["head"], "n_items": d.get("n_items", 0)}
                                     for d in datasets]})
        return out

    def names(self) -> list[str]:
        return sorted(self._table)

    def default_ws(self) -> str:
        if "." in self._table:
            return "."
        return self.names()[0] if self._table else ""

    # ------------------------------------------------------------------ Hub

    def hub(self, ws: str = "") -> Hub:
        """按名字取 Hub；名字留空用默认工作区，名字不认识就报错（绝不去猜路径）。"""
        name = str(ws or "") or self.default_ws()
        if not name:
            raise UnknownWorkspace(
                f"目录不存在或不是文件夹：{self.root}（--data 要指向一个真实目录）")
        row = self._table.get(name)
        if row is None:
            raise UnknownWorkspace(f"未知工作区 {ws!r}，可用 {self.names()}")
        with self._lock:
            hub = self._hubs.get(name)
            if hub is None:
                hub = Hub(row["db"], out_dir=self.out_dir)
                self._hubs[name] = hub
            return hub

    def close(self) -> None:
        with self._lock:
            for hub in self._hubs.values():
                hub.close()
            self._hubs.clear()

    def clear_caches(self) -> int:
        with self._lock:
            hubs = list(self._hubs.values())
        for hub in hubs:
            hub.clear_cache()
        return len(hubs)


def resolve_dataset(hub: Hub, dataset: str = "") -> dict:
    """把 id 或名称解析成数据集；不明确时报一句人话，让界面直接显示。"""
    rows = hub.datasets()
    if not rows:
        raise KeyError("这个工作区还没有数据集，先在管理台新建一个")
    if not dataset and len(rows) == 1:
        return rows[0]
    hit = [d for d in rows if d["id"] == dataset or d["name"] == dataset]
    if len(hit) != 1:
        raise KeyError(f"数据集 {dataset!r} 不明确，可用："
                       + "、".join(f"{d['name']}({d['id']})" for d in rows))
    return hit[0]


def resolve_version(hub: Hub, dataset_id: str, version_id: str = "") -> str:
    """版本留空 = 该数据集最新一版；界面绝大多数请求都不带版本。"""
    if version_id:
        if hub.store.get_version(version_id) is None:
            raise KeyError(f"未知版本 {version_id}")
        return version_id
    head = hub.head_version(dataset_id)
    if not head:
        raise KeyError(f"数据集 {dataset_id} 还没有任何版本")
    return head


def workspace_from_args(db: str = "", data: str = "", out: str = "") -> Workspaces:
    """命令行参数 -> 工作区。--db 给单个 sqlite（其父目录即工作区），--data 给根目录。

    库文件不存在时直接报错，不静默建一个空的：把 `--db mdva.sqllite` 这种拼错
    当成"新建空库"，界面会打开一个看起来数据全丢了的页面。
    """
    if db:
        path = Path(db)
        if path.name != DB_NAME:
            raise ValueError(f"--db 要指向 {DB_NAME} 文件，收到 {path}")
        if not path.is_file():
            raise FileNotFoundError(f"数据库不存在：{path}")
        return Workspaces(path.parent, out_dir=out or None)
    return Workspaces(data, out_dir=out or None)
