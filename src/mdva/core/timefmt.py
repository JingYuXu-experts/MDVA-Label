"""时间显示格式化。被渲染层与报告层共用，故放在 core 而非 io。"""

from __future__ import annotations

import datetime as _dt
import math
from typing import Optional

_UTC = _dt.timezone.utc


def to_datetime(seconds: Optional[float]) -> Optional[_dt.datetime]:
    if seconds is None or not math.isfinite(seconds):
        return None
    try:
        return _dt.datetime.fromtimestamp(seconds, _UTC)
    except (OverflowError, OSError, ValueError):
        return None


def fmt_time(seconds: Optional[float]) -> str:
    dt = to_datetime(seconds)
    if dt is None:
        return "--"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def fmt_clock(seconds: Optional[float]) -> str:
    dt = to_datetime(seconds)
    if dt is None:
        return "--"
    return dt.strftime("%H:%M:%S")


def fmt_iso(seconds: Optional[float]) -> str:
    dt = to_datetime(seconds)
    if dt is None:
        return "--"
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def fmt_axis(seconds: Optional[float], span: Optional[float] = None) -> str:
    """按可视时间跨度自适应精度：放大到秒级才显示毫秒与日期。"""
    dt = to_datetime(seconds)
    if dt is None:
        return "--"
    if span is None:
        return dt.strftime("%H:%M:%S")
    if span < 2:
        return dt.strftime("%H:%M:%S.%f")[:-3]
    if span < 3600:
        return dt.strftime("%H:%M:%S")
    if span < 86400 * 3:
        return dt.strftime("%m-%d %H:%M")
    return dt.strftime("%Y-%m-%d")


def fmt_duration(seconds: float) -> str:
    s = abs(float(seconds))
    if s < 1:
        return f"{s * 1000:.0f}ms"
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(int(s), 60)
    if m < 60:
        return f"{m}m{sec:02d}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"
