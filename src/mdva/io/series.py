"""多通道时序加载：CSV / Parquet / HDF5 / NPY，含遥测标定与重采样。

遥测参数分两类，处理方式不同，混用会让离散量出现无意义的插值：
- 模拟量(analog)：工程值 = 原始码值 * scale + offset
- 离散量(discrete)：码值对应状态枚举，只能整段显示，不能求均值或插值
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Optional

import numpy as np

from ..core.model import DataItem, Modality, Slice, TimeSeries
from .timeutil import to_utc_seconds

TIME_COLUMN_HINTS = (
    "time", "timestamp", "utc", "unix", "epoch", "met", "sod", "mjd",
    "date", "datetime", "date_obs", "obs_date", "t",
)

SERIES_SUFFIXES = (".csv", ".tsv", ".parquet", ".pq", ".h5", ".hdf5", ".npy", ".npz", ".dat", ".txt")


class Calibration:
    """码值 -> 工程值的标定表。

    schema（JSON）:
    {
      "channel_name": {"type": "analog", "scale": 0.001, "offset": 30.0, "unit": "V"},
      "switch_state": {"type": "discrete", "values": {"0": "Nominal", "1": "Safe", "2": "Fault"}}
    }
    """

    def __init__(self, table: Optional[dict] = None):
        self.table = table or {}

    @classmethod
    def load(cls, path) -> "Calibration":
        path = Path(path)
        if path.suffix.lower() == ".json":
            return cls(json.loads(path.read_text(encoding="utf-8")))
        # CSV: channel,type,scale,offset,unit,values(可选 JSON)
        table = {}
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("channel") or row.get("name") or "").strip()
                if not name:
                    continue
                entry = {"type": (row.get("type") or "analog").strip().lower()}
                for key in ("scale", "offset"):
                    if row.get(key) not in (None, ""):
                        entry[key] = float(row[key])
                if row.get("unit"):
                    entry["unit"] = row["unit"].strip()
                if row.get("values"):
                    try:
                        entry["values"] = json.loads(row["values"])
                    except json.JSONDecodeError:
                        entry["values"] = dict(
                            (k.strip(), v.strip())
                            for k, _, v in (x.partition(":") for x in row["values"].split(";"))
                            if k.strip()
                        )
                table[name] = entry
        return cls(table)

    def is_discrete(self, channel: str) -> bool:
        return self.table.get(channel, {}).get("type") == "discrete"

    def unit(self, channel: str) -> str:
        return str(self.table.get(channel, {}).get("unit", ""))

    def labels(self, channel: str) -> dict:
        return {int(k): str(v) for k, v in self.table.get(channel, {}).get("values", {}).items()}

    def apply(self, channel: str, raw: np.ndarray) -> np.ndarray:
        spec = self.table.get(channel)
        if not spec:
            return raw.astype(np.float64)
        if spec.get("type") == "discrete":
            return raw.astype(np.float64)
        scale = float(spec.get("scale", 1.0))
        offset = float(spec.get("offset", 0.0))
        return raw.astype(np.float64) * scale + offset


def _find_time_column(names) -> Optional[int]:
    lowered = [str(n).strip().lower() for n in names]
    for hint in TIME_COLUMN_HINTS:
        if hint in lowered:
            return lowered.index(hint)
    return None


def _parse_time_column(values, start: Optional[float], dt: Optional[float]) -> np.ndarray:
    """时间列可能是日期串、绝对 UTC 秒，也可能是 MET/SOD 相对秒。

    四种情况的兜底顺序不能反：把相对秒当绝对时间会让数据掉到 1970 年，
    把日期当相对秒会溢出，都无法察觉，只能靠量级判断。
    """
    parsed = np.array([to_utc_seconds(v) for v in values], dtype=np.float64)
    if parsed.size and np.all(np.isfinite(parsed)):
        if parsed.max() < 1e9:
            return parsed + (start or 0.0)
        return parsed

    try:
        nums = np.array([float(v) for v in values], dtype=np.float64)
    except (TypeError, ValueError):
        nums = None
    if nums is not None and nums.size and np.all(np.isfinite(nums)):
        return (start or 0.0) + nums

    base = start if start is not None else 0.0
    return base + np.arange(len(values), dtype=np.float64) * float(dt or 1.0)


def _read_table(uri: str) -> tuple:
    """返回 (列名列表, 列数据列表, 时间列索引或 None)。"""
    path = Path(uri)
    suffix = path.suffix.lower()

    if suffix in (".csv", ".tsv", ".dat", ".txt"):
        delim = "\t" if suffix == ".tsv" else None
        with path.open(encoding="utf-8-sig", newline="") as fh:
            sample = fh.read(4096)
            fh.seek(0)
            if delim is None:
                try:
                    delim = csv.Sniffer().sniff(sample, delimiters=",;\t ").delimiter
                except csv.Error:
                    # 单列文件嗅不出分隔符，退回逗号即可
                    delim = ","
            rows = list(csv.reader(fh, delimiter=delim))
        if not rows:
            return [], [], None
        header = rows[0]
        looks_like_header = any(cell.replace(".", "", 1).lstrip("-").isdigit() for cell in header)
        if looks_like_header:
            header = [f"ch{i}" for i in range(len(rows[0]))]
            body = rows
        else:
            body = rows[1:]
        ncol = len(header)
        cols = [[] for _ in range(ncol)]
        for row in body:
            for i in range(ncol):
                cols[i].append(row[i] if i < len(row) else "")
        return header, cols, _find_time_column(header)

    if suffix in (".parquet", ".pq"):
        import pandas as pd

        frame = pd.read_parquet(path)
        header = list(frame.columns)
        cols = [frame[c].to_numpy() for c in header]
        return header, cols, _find_time_column(header)

    if suffix in (".h5", ".hdf5"):
        import h5py

        with h5py.File(path, "r") as fh:
            arrays = {}

            def visit(name, obj):
                if isinstance(obj, h5py.Dataset) and obj.ndim <= 2:
                    arrays[name] = obj[()]

            fh.visititems(visit)
            time_key = next((k for k in arrays if k.lower() in ("time", "t", "timestamp", "utc")), None)
            header = [k for k in arrays if k != time_key]
            cols = [arrays[k] for k in header]
            time_col = arrays[time_key] if time_key else None
        if time_col is not None:
            return header, cols + [time_col], len(cols)
        return header, cols, None

    if suffix == ".npy":
        arr = np.load(path)
        if arr.ndim == 1:
            return ["ch0"], [arr], None
        header = [f"ch{i}" for i in range(arr.shape[1])]
        return header, [arr[:, i] for i in range(arr.shape[1])], None

    if suffix == ".npz":
        with np.load(path) as zf:
            keys = list(zf.keys())
            time_key = next((k for k in keys if k.lower() in ("time", "t", "timestamp")), None)
            data_keys = [k for k in keys if k != time_key]
            cols = [zf[k] for k in data_keys]
            if time_key:
                return data_keys, cols + [zf[time_key]], len(cols)
            return data_keys, cols, None

    raise ValueError(f"不支持的时序文件格式: {suffix} ({uri})")


def load_series(
    uri: str,
    title: Optional[str] = None,
    start: Optional[float] = None,
    dt: Optional[float] = 1.0,
    calibration: Optional[Calibration] = None,
    time_col: Optional[str] = None,
) -> DataItem:
    path = Path(uri)
    header, cols, t_idx = _read_table(str(path))
    if time_col is not None and time_col in header:
        t_idx = header.index(time_col)

    if t_idx is None:
        n = max((len(c) for c in cols), default=0)
        t = (start or 0.0) + np.arange(n, dtype=np.float64) * float(dt or 1.0)
        data_cols = list(cols)
    else:
        raw_time = cols[t_idx]
        t = _parse_time_column(raw_time, start, dt)
        data_cols = [c for i, c in enumerate(cols) if i != t_idx]
        header = [h for i, h in enumerate(header) if i != t_idx]

    calib = calibration or Calibration()
    n = len(t)
    if n == 0:
        raise ValueError(f"{uri} 没有可用样本")

    values = np.full((n, max(len(data_cols), 1)), np.nan, dtype=np.float64)
    discrete = []
    units = []
    for j, name in enumerate(header):
        col = data_cols[j]
        arr = np.asarray(col)
        numeric = arr.astype(np.float64) if arr.dtype.kind in ("f", "i", "u", "b") else _coerce_numeric(arr)
        numeric = calib.apply(str(name), numeric)
        values[: min(n, numeric.size), j] = numeric[: min(n, numeric.size)]
        discrete.append(calib.is_discrete(str(name)))
        units.append(calib.unit(str(name)))

    order = np.argsort(t, kind="stable")
    t = t[order]
    values = values[order]
    channels = [str(h) for h in header]

    def opener():
        return TimeSeries(
            t=t,
            values=values,
            channels=channels,
            units=units,
            discrete=discrete,
        )

    step = float(np.median(np.diff(t))) if n > 1 else float(dt or 1.0)
    return DataItem(
        id=path.name,
        modality=Modality.SERIES.value,
        uri=str(path),
        title=title or path.name,
        t_ref=float(t[0]),
        t_end=float(t[-1]),
        meta={
            "format": path.suffix.lstrip("."),
            "n_samples": n,
            "n_channels": len(channels),
            "channels": channels,
            "units": units,
            "discrete": discrete,
            "median_step": step,
        },
        shape=(n, len(channels)),
        dtype="float64",
        n_views=len(channels),
        _opener=opener,
    )


def _coerce_numeric(arr) -> np.ndarray:
    out = np.full(len(arr), np.nan, dtype=np.float64)
    for i, v in enumerate(arr):
        try:
            out[i] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def resample(ts: TimeSeries, step: float, agg: str = "mean", max_gap: Optional[int] = None) -> TimeSeries:
    """不规则采样 -> 等间隔。

    分箱方向必须是"样本落到格点"而不是"格点找样本"：后者每个格点只取
    一个样本，均值退化成抽样，聚合语义就丢了。
    """
    if ts.t.size == 0 or step <= 0:
        return ts
    t0, t1 = float(ts.t[0]), float(ts.t[-1])
    n_bins = max(1, int(math.floor((t1 - t0) / step)) + 1)
    grid = t0 + np.arange(n_bins, dtype=np.float64) * step

    b = np.floor((ts.t - t0) / step).astype(np.int64)
    np.clip(b, 0, n_bins - 1, out=b)

    out = np.full((n_bins, ts.n_channels), np.nan, dtype=np.float64)
    force_nearest = agg in ("last", "nearest")

    for ch in range(ts.n_channels):
        col = ts.values[:, ch]
        keep = np.isfinite(col)
        if not keep.any():
            continue
        bkt, val = b[keep], col[keep]

        # 离散量求均值会造出不存在的中间状态，取箱内最后一个样本
        if force_nearest or ts.is_discrete(ch):
            out[bkt, ch] = val
        elif agg == "max":
            acc = np.full(n_bins, -np.inf)
            np.maximum.at(acc, bkt, val)
            out[:, ch] = np.where(np.isneginf(acc), np.nan, acc)
        elif agg == "min":
            acc = np.full(n_bins, np.inf)
            np.minimum.at(acc, bkt, val)
            out[:, ch] = np.where(np.isposinf(acc), np.nan, acc)
        elif agg == "sum":
            counts = np.bincount(bkt, minlength=n_bins)
            out[:, ch] = np.where(counts > 0, np.bincount(bkt, weights=val, minlength=n_bins), np.nan)
        else:
            counts = np.bincount(bkt, minlength=n_bins)
            sums = np.bincount(bkt, weights=val, minlength=n_bins)
            out[:, ch] = np.where(counts > 0, sums / np.maximum(counts, 1), np.nan)

    if max_gap:
        out = _mask_long_gaps(out, max_gap)

    return TimeSeries(t=grid, values=out, channels=list(ts.channels),
                      units=list(ts.units), discrete=list(ts.discrete))


def _mask_long_gaps(values: np.ndarray, max_gap: int) -> np.ndarray:
    """连续缺失超过 max_gap 个格点时断开，避免跨数据缺口连线。"""
    out = values.copy()
    for ch in range(out.shape[1]):
        bad = ~np.isfinite(out[:, ch])
        if not bad.any():
            continue
        edges = np.flatnonzero(np.diff(np.concatenate(([0], bad.view(np.int8), [0]))))
        for start, stop in zip(edges[::2], edges[1::2]):
            if stop - start > max_gap:
                out[start:stop, ch] = np.nan
    return out


def load(uri: str, **kwargs) -> DataItem:
    if kwargs.get("calibration") and isinstance(kwargs["calibration"], (str, Path)):
        kwargs["calibration"] = Calibration.load(kwargs["calibration"])
    return load_series(uri, **kwargs)


def register(registry) -> None:
    registry.add(
        "series", load_series, "多通道时序（CSV/Parquet/HDF5/NPY）",
        {
            "start": {"type": "number", "default": None, "description": "相对时间列的基准 UTC 秒"},
            "dt": {"type": "number", "default": 1.0, "description": "无时间列时的采样间隔秒"},
            "calibration": {"type": "string", "default": None, "description": "标定表 JSON/CSV 路径"},
            "time_col": {"type": "string", "default": None, "description": "指定时间列名"},
        },
    )
