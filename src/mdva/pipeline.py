"""L3 并行分析流水线：配置驱动的批量处理。

补齐题目里"并行分析"的计算并行那一半。两个设计约束：

1. 跨进程只能传可序列化的描述，不能传 DataItem——它的 _opener 是闭包，
   进 ProcessPoolExecutor 会直接 pickle 失败。因此任务参数一律是
   (uri, modality, load_kwargs)，由 worker 自己重建数据项。
2. 单个文件出错不能中断整批。例行数据处理里一个坏文件很常见，
   整批失败等于这一轮白跑。
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from . import analysis, io, render
from .core.model import Slice

JSON_SAFE = (int, float, str, bool, type(None))


@dataclass
class Stage:
    analyzer: str
    params: dict = field(default_factory=dict)

    @classmethod
    def from_obj(cls, obj) -> "Stage":
        if isinstance(obj, str):
            return cls(obj)
        if isinstance(obj, dict):
            name = obj.get("analyzer") or obj.get("name")
            if not name:
                raise ValueError(f"stage 缺少 analyzer 字段: {obj}")
            params = dict(obj.get("params") or {})
            for key in list(obj):
                if key not in ("analyzer", "name", "params"):
                    params[key] = obj[key]
            return cls(str(name), params)
        raise TypeError(f"无法解析 stage: {obj!r}")


@dataclass
class PipelineSpec:
    source: str
    out: str = "mdva_batch"
    stages: list = field(default_factory=list)
    uris: list = field(default_factory=list)
    modality: Optional[str] = None
    load_kwargs: dict = field(default_factory=dict)
    render_style: dict = field(default_factory=dict)
    window: Optional[tuple] = None
    index: int = 0
    workers: int = 0
    executor: str = "thread"
    limit: Optional[int] = None
    draw: bool = True
    cell: tuple = (480, 300)
    cols: int = 4
    recursive: bool = True

    @property
    def slice(self) -> Slice:
        if self.window is None:
            return Slice(index=int(self.index))
        t0, t1 = self.window
        return Slice(index=int(self.index), t0=float(t0), t1=float(t1))

    @classmethod
    def from_obj(cls, obj: dict) -> "PipelineSpec":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(obj) - known - {"steps", "pipeline"}
        if unknown:
            print(f"[warn] 配置中的未知字段将被忽略: {sorted(unknown)}")
        stages = obj.get("stages") or obj.get("steps") or []
        payload = {k: v for k, v in obj.items() if k in known and k != "stages" and k != "steps"}
        payload["stages"] = [Stage.from_obj(s) for s in stages]
        payload["cell"] = tuple(payload.get("cell", (480, 300)))
        return cls(**payload)


def load_spec(path) -> PipelineSpec:
    """支持 JSON 与 YAML（需要 pyyaml）。"""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:
            raise ImportError("读取 YAML 配置需要 pyyaml，或改用 .json 配置") from exc
        obj = yaml.safe_load(text)
    else:
        obj = json.loads(text)
    return PipelineSpec.from_obj(obj)


def _run_one(uri: str, modality: Optional[str], load_kwargs: dict,
             stages: list, window: Optional[tuple], index: int = 0) -> dict:
    """worker 体。必须是模块级函数且参数可序列化，否则进程池不可用。"""
    record = {"uri": uri, "item": Path(uri).name, "ok": False, "metrics": {},
              "flags": [], "error": "", "elapsed": 0.0, "modality": "", "t_ref": None}
    t_start = time.perf_counter()
    try:
        item = io.load_any(uri, modality=modality, **load_kwargs)
        if window:
            sl = Slice(index=int(index), t0=float(window[0]), t1=float(window[1]))
        else:
            sl = Slice(index=int(index))
        record["modality"] = item.modality
        record["t_ref"] = item.t_ref
        record["title"] = item.title

        for stage in stages:
            name = stage.analyzer if isinstance(stage, Stage) else stage["analyzer"]
            params = stage.params if isinstance(stage, Stage) else (stage.get("params") or {})
            if name not in analysis.names():
                record["error"] = f"未知算子 {name}"
                return record
            inst = analysis.get(name, **params)
            if not inst.handles(item):
                continue
            res = inst.run(item, sl)
            for k, v in res.metrics.items():
                record["metrics"][f"{name}.{k}"] = v if isinstance(v, JSON_SAFE) else str(v)
            for f in res.flags[:50]:
                record["flags"].append({"analyzer": name, "t": f.t, "channel": f.channel,
                                        "label": f.label, "score": f.score})
        record["ok"] = True
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["elapsed"] = time.perf_counter() - t_start
    return record


def run(spec: PipelineSpec, progress: bool = True) -> dict:
    """执行批量分析，返回 {"records": [...], "summary": {...}}。

    `spec.uris` 非空时按显式清单跑（界面上勾选的那几路），否则扫描 `spec.source`。
    """
    if spec.uris:
        uris = [str(u) for u in spec.uris]
        if spec.limit:
            uris = uris[: spec.limit]
    else:
        uris = [i.uri for i in io.scan(spec.source, recursive=spec.recursive, limit=spec.limit)]
    if not uris:
        return {"records": [], "summary": {"n_items": 0, "n_ok": 0, "n_failed": 0,
                                           "elapsed": 0.0, "workers": 0, "throughput": 0.0}}

    stages = [Stage(s["analyzer"], dict(s.get("params") or {})) if isinstance(s, dict) else s
              for s in spec.stages]
    if not stages:
        raise ValueError("流水线没有任何 stage")

    workers = spec.workers or min(len(uris), max(1, (os.cpu_count() or 2)))
    workers = max(1, min(workers, len(uris)))
    args = [(u, spec.modality, dict(spec.load_kwargs), stages, spec.window, spec.index)
            for u in uris]

    t0 = time.perf_counter()
    if workers == 1:
        records = [_run_one(*a) for a in args]
    else:
        pool_cls = ProcessPoolExecutor if spec.executor == "process" else ThreadPoolExecutor
        with pool_cls(max_workers=workers) as pool:
            # 用 submit 而不是 map：map 只传单个位置参数，元组不会被展开成多参数
            futures = [pool.submit(_run_one, *a) for a in args]
            records = [f.result() for f in futures]
    elapsed = time.perf_counter() - t0

    ok = [r for r in records if r["ok"]]
    failed = [r for r in records if not r["ok"]]
    for r in failed:
        print(f"[warn] {Path(r['uri']).name}: {r['error']}")

    summary = {
        "n_items": len(records),
        "n_ok": len(ok),
        "n_failed": len(failed),
        "workers": workers,
        "executor": "serial" if workers == 1 else spec.executor,
        "elapsed": round(elapsed, 4),
        "throughput": round(len(records) / elapsed, 2) if elapsed > 0 else 0.0,
        "items_per_minute": round(len(records) * 60 / elapsed, 1) if elapsed > 0 else 0.0,
        "modality_counts": _counts(r["modality"] for r in records),
        "n_flags": sum(len(r["flags"]) for r in ok),
    }
    if progress:
        print(f"批量完成：{summary['n_ok']}/{summary['n_items']} 成功，"
              f"{workers} 并发 {summary['elapsed']}s，{summary['throughput']} 项/秒")

    if spec.draw:
        _write_visuals(spec, records)
    _write_report(spec, records, summary)
    return {"records": records, "summary": summary}


def _counts(values) -> dict:
    out: dict = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def measure_speedup(spec: PipelineSpec, workers: int = 0) -> dict:
    """同一批数据分别跑串行与并发，给出加速比。第 6 章并行实验用这个。"""
    import copy

    serial = copy.deepcopy(spec)
    serial.workers = 1
    serial.draw = False
    t0 = time.perf_counter()
    s = run(serial, progress=False)
    serial_time = time.perf_counter() - t0

    par = copy.deepcopy(spec)
    par.workers = workers or par.workers or 0
    par.draw = False
    t1 = time.perf_counter()
    p = run(par, progress=False)
    par_time = time.perf_counter() - t1

    n = workers or par.workers or s["summary"]["workers"]
    return {
        "n_items": s["summary"]["n_items"],
        "workers": p["summary"]["workers"],
        "requested_workers": n,
        "serial_seconds": round(serial_time, 4),
        "parallel_seconds": round(par_time, 4),
        "speedup": round(serial_time / par_time, 3) if par_time > 0 else 0.0,
        "same_results": _records_match(s["records"], p["records"]),
    }


def _records_match(a: list, b: list) -> bool:
    if len(a) != len(b):
        return False
    key = lambda r: (Path(r["uri"]).name, r["ok"], r.get("error", ""))
    if sorted(map(key, a)) != sorted(map(key, b)):
        return False
    for ra, rb in zip(sorted(a, key=key), sorted(b, key=key)):
        if set(ra["metrics"]) != set(rb["metrics"]):
            return False
        for k in ra["metrics"]:
            va, vb = ra["metrics"][k], rb["metrics"][k]
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                if not np.isclose(va, vb, rtol=1e-9, atol=1e-12, equal_nan=True):
                    return False
            elif va != vb:
                return False
    return True


def _write_report(spec: PipelineSpec, records: list, summary: dict) -> None:
    out = Path(spec.out)
    out.mkdir(parents=True, exist_ok=True)
    payload = {"summary": summary, "records": _jsonable(records)}
    (out / "batch_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = [r for r in records if r["metrics"]]
    if rows:
        keys = sorted({k for r in rows for k in r["metrics"]})
        lines = ["item,modality,ok,elapsed_s," + ",".join(keys)]
        for r in rows:
            cells = [r["item"], r["modality"], str(r["ok"]), f"{r['elapsed']:.4f}"]
            cells += [_cell(r["metrics"].get(k)) for k in keys]
            lines.append(",".join(cells))
        (out / "metrics.csv").write_text("\n".join(lines), encoding="utf-8")


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return "" if not np.isfinite(v) else f"{v:.6g}"
    return str(v).replace(",", ";")


def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return {"__ndarray__": list(obj.shape), "dtype": str(obj.dtype)}
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj) if np.isfinite(obj) else None
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    return obj


def _write_visuals(spec: PipelineSpec, records: list) -> None:
    """为成功项生成标注图并拼一张总览，对应答辩演示的"并行对比"。"""
    from PIL import Image

    out = Path(spec.out) / "annotated"
    out.mkdir(parents=True, exist_ok=True)
    tiles = []
    for r in records:
        if not r["ok"]:
            continue
        try:
            item = io.load_any(r["uri"], modality=spec.modality, **spec.load_kwargs)
            result = render.render_item(item, size=spec.cell, window=spec.slice,
                                        style=dict(spec.render_style))
            tiles.append(result)
            # 多模型输出的文件名完全相同，落盘必须带上来源目录名
            src = Path(r["uri"])
            name = f"{src.parent.name}_{src.name}".replace(" ", "_")
            if not name.lower().endswith(".png"):
                name += ".png"
            Image.fromarray(np.ascontiguousarray(result.composited())).save(str(out / name))
            r["annotated"] = str(out / name)
        except Exception as exc:
            print(f"[warn] 出图失败 {Path(r['uri']).name}: {exc}")
    if not tiles:
        return

    cols = max(1, min(spec.cols, len(tiles)))
    rows = int(np.ceil(len(tiles) / cols))
    img = render.render_mosaic(tiles, rows=rows, cols=cols, cell=spec.cell)
    Image.fromarray(img).save(str(out / "overview.png"))
