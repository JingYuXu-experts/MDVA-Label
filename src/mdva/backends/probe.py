"""机器档位探测：决定哪些后端在这台机器上跑得动。

完全离线、纯 CPU 是本设计的硬约束，所以这里**不探测显卡**——
`has_gpu` 恒为 False，并在 note 里说明"本机可能有独显，但本平台不使用它"，
避免界面给出一个永远不会被用到的开关。
内存与核数是真的要用来做决定的：并发数、进程/线程选择、
以及"这一路 20000 点遥测能不能整段读进来"。
"""

from __future__ import annotations

import ctypes
import os
import platform
import re
import shutil
from dataclasses import dataclass, field


@dataclass
class Probe:
    cpu_count: int = 1
    ram_gb: float = 0.0
    has_gpu: bool = False
    python: str = ""
    machine: str = ""
    free_disk_gb: float = 0.0
    note: str = "本平台不接入深度学习后端，显卡不参与计算"
    extras: dict = field(default_factory=dict)

    def tier(self) -> str:
        """粗粒度档位，供推荐器扣分项与并发上限使用。"""
        if self.ram_gb >= 32 and self.cpu_count >= 8:
            return "high"
        if self.ram_gb >= 16 and self.cpu_count >= 4:
            return "mid"
        return "low"

    def max_workers(self, n_items: int = 0, cap: int = 8) -> int:
        limit = max(1, min(cap, self.cpu_count))
        return max(1, min(limit, n_items)) if n_items else limit

    def can_load_points(self, n_points: int, n_channels: int = 1) -> bool:
        """float64 × (点, 通道) 再加一份工作副本，按半张内存上限估。"""
        need_gb = n_points * max(1, n_channels) * 8 * 2 / (1024 ** 3)
        return need_gb <= max(0.5, self.ram_gb * 0.5)

    def to_json(self) -> dict:
        return {"cpu_count": self.cpu_count, "ram_gb": round(self.ram_gb, 2),
                "has_gpu": self.has_gpu, "python": self.python, "machine": self.machine,
                "free_disk_gb": round(self.free_disk_gb, 2), "tier": self.tier(),
                "note": self.note}


def _ram_bytes() -> float:
    system = platform.system()
    if system == "Windows":
        try:
            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return float(stat.ullTotalPhys)
        except Exception:
            return 0.0
    elif system == "Linux":
        try:
            text = open("/proc/meminfo", encoding="ascii").read()
            m = re.search(r"MemTotal:\s+(\d+)\s+kB", text)
            if m:
                return float(m.group(1)) * 1024.0
        except OSError:
            return 0.0
    else:
        try:
            page = os.sysconf("SC_PAGE_SIZE")
            pages = os.sysconf("SC_PHYS_PAGES")
            return float(page * pages)
        except (ValueError, KeyError, OSError):
            return 0.0
    return 0.0


def detect(path: str = ".") -> Probe:
    """读一次本机配置。任何一项拿不到就退化成保守值，不抛异常。"""
    try:
        free = shutil.disk_usage(path).free / (1024 ** 3)
    except OSError:
        free = 0.0
    return Probe(
        cpu_count=os.cpu_count() or 1,
        ram_gb=_ram_bytes() / (1024 ** 3),
        has_gpu=False,
        python=platform.python_version(),
        machine=f"{platform.system()}/{platform.machine()}",
        free_disk_gb=free,
    )


__all__ = ["Probe", "detect"]
