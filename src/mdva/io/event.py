"""事件模态加载：告警日志、指令序列、观测窗口。

事件是时间标签数据，本身不画曲线，但它是把"遥测异常"和"操作动作"
对上号的载体，因此必须与时序共用同一套 UTC 秒时间基准。
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Optional

import numpy as np

from ..core.model import DataItem, EventRow, EventTable, Modality

EVENT_SUFFIXES = (".json", ".jsonl", ".csv", ".tsv", ".log")

TIME_KEYS = ("t", "time", "timestamp", "utc", "datetime", "date", "event_time", "sod", "met")
LABEL_KEYS = ("label", "name", "event", "message", "desc", "description", "text", "msg")
LEVEL_KEYS = ("level", "severity", "priority", "class")
SOURCE_KEYS = ("source", "subsys", "subsystem", "channel", "param", "origin")
END_KEYS = ("t_end", "end", "end_time", "stop", "duration_end")
DURATION_KEYS = ("duration", "dur", "elapsed")

_LEVEL_WORDS = {"info": 0, "notice": 1, "warning": 2, "warn": 2, "alarm": 3,
                "error": 3, "critical": 4, "fault": 4, "emergency": 5}


def _pick(d: dict, keys) -> Optional[object]:
    lowered = {str(k).lower(): v for k, v in d.items()}
    for key in keys:
        if key in lowered and lowered[key] not in (None, ""):
            return lowered[key]
    return None


def _level_of(value) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return _LEVEL_WORDS.get(str(value).strip().lower(), 0)


def _row_from_dict(d: dict, start: Optional[float], dt: float) -> Optional[EventRow]:
    raw_t = _pick(d, TIME_KEYS)
    if raw_t is None:
        return None
    t = to_seconds(raw_t, start, dt)
    if t is None:
        return None
    label = _pick(d, LABEL_KEYS)
    end_raw = _pick(d, END_KEYS)
    dur_raw = _pick(d, DURATION_KEYS)
    if end_raw is not None:
        t_end = to_seconds(end_raw, start, dt) or math.inf
    elif dur_raw is not None:
        try:
            t_end = t + float(dur_raw)
        except (TypeError, ValueError):
            t_end = math.inf
    else:
        t_end = math.inf
    return EventRow(
        t=t,
        label=str(label) if label is not None else "event",
        level=_level_of(_pick(d, LEVEL_KEYS)),
        source=str(_pick(d, SOURCE_KEYS) or ""),
        t_end=t_end,
        extra={k: v for k, v in d.items() if str(k).lower() not in
               {kk for kk in (*TIME_KEYS, *LABEL_KEYS, *LEVEL_KEYS, *SOURCE_KEYS, *END_KEYS, *DURATION_KEYS)}},
    )


def to_seconds(raw, start: Optional[float], dt: float) -> Optional[float]:
    """事件时刻可能是绝对日期，也可能是帧号/相对秒。"""
    from .timeutil import to_utc_seconds

    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        if value >= 1e9:
            return value
        return (start or 0.0) + value if math.isfinite(value) else None
    parsed = to_utc_seconds(raw)
    if parsed is not None:
        return parsed
    try:
        frames = float(raw)
    except (TypeError, ValueError):
        return None
    return (start or 0.0) + frames * dt


def load_events(
    uri: str,
    title: Optional[str] = None,
    start: Optional[float] = None,
    dt: float = 1.0,
) -> DataItem:
    path = Path(uri)
    suffix = path.suffix.lower()
    rows = []

    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload = payload.get("events") or payload.get("rows") or [payload]
        rows = [_row_from_dict(item, start, dt) for item in payload if isinstance(item, dict)]
    elif suffix == ".jsonl":
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(_row_from_dict(json.loads(line), start, dt))
            except json.JSONDecodeError:
                continue
    elif suffix in (".csv", ".tsv"):
        delim = "\t" if suffix == ".tsv" else ","
        with path.open(encoding="utf-8-sig", newline="") as fh:
            rows = [_row_from_dict(dict(r), start, dt) for r in csv.DictReader(fh, delimiter=delim)]
    else:
        # 非结构日志：`<时间> [级别] 文本`
        rows = _load_plain_log(path, start, dt)

    events = EventTable([r for r in rows if r is not None])
    levels = {r.level for r in events.rows}

    def opener():
        return events

    return DataItem(
        id=path.name,
        modality=Modality.EVENT.value,
        uri=str(path),
        title=title or path.name,
        t_ref=events.start if math.isfinite(events.start) else start,
        t_end=events.end if math.isfinite(events.end) else None,
        meta={
            "format": path.suffix.lstrip("."),
            "n_events": len(events.rows),
            "levels": sorted(levels),
            "sources": sorted({r.source for r in events.rows if r.source}),
        },
        shape=(len(events.rows),),
        dtype="event",
        # 视图 0 是"全部等级"，之后每个等级一屏；界面按 n_views 出翻页控件，
        # 所以不能出现"有控件但切不出东西"的视图
        n_views=len(levels) + 1 if levels else 1,
        _opener=opener,
    )


def _load_plain_log(path: Path, start: Optional[float], dt: float) -> list:
    import re

    pattern = re.compile(
        r"^\s*(?P<t>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}|\d+\.?\d*)\s*"
        r"(?:\[(?P<lv>[A-Za-z]+)\])?\s*(?P<msg>.*)$"
    )
    rows = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        m = pattern.match(line)
        if not m:
            continue
        rows.append(_row_from_dict(
            {"t": m.group("t"), "level": m.group("lv"), "label": m.group("msg")}, start, dt
        ))
    return rows


def load(uri: str, **kwargs) -> DataItem:
    return load_events(uri, **kwargs)


def register(registry) -> None:
    registry.add(
        "event", load_events, "事件表（JSON/JSONL/CSV/文本日志）",
        {
            "start": {"type": "number", "default": None, "description": "相对时刻的基准 UTC 秒"},
            "dt": {"type": "number", "default": 1.0, "description": "帧号到秒的换算间隔"},
        },
    )
