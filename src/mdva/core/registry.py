"""注册表：加载器 / 渲染器 / 算子的可插拔机制。

沿用上游 custom_func 的"目录扫描 + 热加载"思路，但把接口从
`custom_process_img(img)` 泛化为带元信息的显式注册，使前端能自动
生成参数面板、CLI 能校验配置。
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
from typing import Callable, Optional


class Entry:
    __slots__ = ("name", "factory", "description", "schema")

    def __init__(self, name, factory, description, schema):
        self.name = name
        self.factory = factory
        self.description = description
        self.schema = schema

    def create(self, **kwargs):
        return self.factory(**kwargs)

    def __repr__(self):
        return f"Entry({self.name!r})"


class Registry:
    def __init__(self, kind: str):
        self.kind = kind
        self._items: dict = {}

    def add(self, name: str, factory: Callable, description: str = "", schema: Optional[dict] = None) -> None:
        # 同名覆盖而非报错：插件目录被重复扫描时应保持幂等
        self._items[name] = Entry(name, factory, description, schema or {})

    def get(self, name: str) -> "Entry":
        try:
            return self._items[name]
        except KeyError:
            raise KeyError(f"未注册的{self.kind}: {name}（可用: {self.names()}）") from None

    def names(self) -> list:
        return sorted(self._items)

    def __contains__(self, name: str) -> bool:
        return name in self._items

    def __iter__(self):
        return iter(self._items[name] for name in self.names())

    def decorator(self, name: str = "", description: str = "", schema: Optional[dict] = None):
        """把类或工厂函数登记进表，属性缺省从对象自身的 name/description/schema 取。"""

        def wrap(obj):
            key = name or getattr(obj, "name", None) or obj.__name__
            self.add(
                key, obj,
                description or getattr(obj, "description", ""),
                schema or getattr(obj, "schema", None) or {},
            )
            obj._mdva_registered = True
            return obj

        return wrap

    def load_plugins(self, directory) -> list:
        """扫描目录下的 .py 文件，执行其中对注册接口的调用。"""
        directory = Path(directory)
        loaded = []
        if not directory.is_dir():
            return loaded
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            if self._exec_module(path):
                loaded.append(path.stem)
        return loaded

    def _exec_module(self, path: Path) -> bool:
        spec = importlib.util.spec_from_file_location(f"mdva_plugin_{path.stem}", path)
        if spec is None or spec.loader is None:
            return False
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            print(f"[warn] 插件 {path.name} 加载失败: {exc}")
            return False
        _, registered = inspect.getmembers(module, lambda o: getattr(o, "_mdva_registered", False))
        return bool(registered)


LOADERS = Registry("加载器")
RENDERERS = Registry("渲染器")
ANALYZERS = Registry("算子")


def make_register_func(registry: Registry):
    """生成模块级装饰器 `register(name, description, schema)`，供算子文件使用。"""

    def register(name: str = "", description: str = "", schema: Optional[dict] = None):
        return registry.decorator(name, description, schema)

    return register
