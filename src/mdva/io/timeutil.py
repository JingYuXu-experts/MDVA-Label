"""时间基准工具：把各模态的时间统一到 UTC 秒。"""

from __future__ import annotations

import datetime as _dt
import math
from typing import Optional

_UNIX_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)

# FITS/卫星数据常见时间格式
_PATTERNS = (
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y/%m/%d %H:%M:%S",
    "%d/%m/%y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%Y%m%d%H%M%S",
    "%Y%m%d.%H%M%S",
    "%Y-%m-%d",
    "%Y%m%d",
)


#: 纯数字日期模式必须按位数认领。strptime 的字段宽度是贪婪的，
#: "%Y%m%d%H%M%S" 会把 10 位 Unix 秒 "1735689600" 切成 1735-6-8 09:06:00，
#: 而同桌的 "1735689660" 又因秒数 60 非法而走对分支——同一列时间一半在 1735 年，
#: 跨模态联动就废了，所以这里硬性限定位数。
_EXACT_DIGITS = {"%Y%m%d%H%M%S": 14, "%Y%m%d": 8}


def to_utc_seconds(value) -> Optional[float]:
    """任意常见时间表示 -> UTC 秒（float）。无法解析时返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if math.isfinite(v) else None
    if isinstance(value, _dt.datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
        return (dt - _UNIX_EPOCH).total_seconds()
    if isinstance(value, _dt.timedelta):
        return value.total_seconds()

    text = str(value).strip()
    if not text:
        return None
    # FITS 允许省略尾部字段，逐项裁剪后重试
    for candidate in (text, text.replace("T", " ")):
        for pattern in _PATTERNS:
            exact = _EXACT_DIGITS.get(pattern)
            if exact is not None and not (candidate.isdigit() and len(candidate) == exact):
                continue
            try:
                dt = _dt.datetime.strptime(candidate, pattern)
            except ValueError:
                continue
            return (dt.replace(tzinfo=_dt.timezone.utc) - _UNIX_EPOCH).total_seconds()
    try:
        dt = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        dt = None
    if dt is not None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return (dt.astimezone(_dt.timezone.utc) - _UNIX_EPOCH).total_seconds()

    # 纯数字字符串：只接受量级上像绝对时间戳的，否则交回调用方按相对秒处理
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if math.isfinite(value) and abs(value) >= 1e8:
        return value
    return None


def from_utc_seconds(seconds: Optional[float]) -> str:
    """UTC 秒 -> 可读 ISO 串，供界面与报告使用。"""
    if seconds is None or not math.isfinite(seconds):
        return "--"
    return _dt.datetime.fromtimestamp(seconds, _dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def iso_from_utc(seconds: Optional[float]) -> str:
    if seconds is None or not math.isfinite(seconds):
        return "--"
    return _dt.datetime.fromtimestamp(seconds, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def guess_epoch_seconds(raw: float, dt_step: float) -> Optional[float]:
    """仅给出相对时刻（如轨道时间、帧序号）时，猜测其 Unix 秒量级。

    遥测里 t 既可能是绝对 UTC 秒，也可能是 0 起的相对秒，量级判断错了
    会导致跨模态对齐完全失败，因此显式保留这个转换点。
    """
    if raw is None or not math.isfinite(raw):
        return None
    if raw > 1e9:
        return float(raw)
    return None
