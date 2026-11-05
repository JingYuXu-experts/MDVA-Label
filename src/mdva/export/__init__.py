"""L4 导出层：把一个标注版本变成可直接训练的数据集。

四种格式都注册进同一张表（`EXPORTERS`），CLI 与 Web API 只认格式名；
`export()` 顺手在任务表里留一条 kind="export" 的记录，manifest 的 exports
段就是从这张表派生的。导入本包即完成内置导出器注册。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from . import base, coco, multimodal, split, train_script, validate, voc, yolo
from .base import (Dataset, EXPORTERS, Sample, load_dataset, read_report, register,
                   write_report)

#: 未指定 out_dir 时的落点。放在数据集目录之外——`scan()` 会递归读目录，
#: 导出结果写进数据目录里等于给自己造出一堆假数据项。
DEFAULT_ROOT = Path("out")


def formats() -> list:
    return EXPORTERS.names()


def schema_of(fmt: str) -> dict:
    return EXPORTERS.get(fmt).schema


def default_out_dir(dataset_name: str, version_id: str, fmt: str,
                    root=DEFAULT_ROOT) -> Path:
    """默认落到 `<root>/exports/<数据集>/<版本>/<格式>`，不同格式互不覆盖。"""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(dataset_name))
    return Path(root) / "exports" / (safe or "dataset") / str(version_id) / fmt


def export(store, version_id: str, fmt: str, out_dir=None, root=None,
           make_scripts: bool = False, task_id: Optional[str] = None, **params) -> dict:
    """导出一种格式，并在任务表里留痕。

    `task_id` 传已有任务行时复用它（清洗/增强任务顺带导出结果就用这条），
    否则新建一条 kind="export" 的任务。参数名特意避开 `task`：YOLO 导出器自己
    就有个 task=detect/segment，两个含义撞名会静默吃掉用户的选择。
    """
    entry = EXPORTERS.get(fmt)
    version = store.get_version(version_id)
    if version is None:
        raise KeyError(f"未知版本 {version_id}")
    dataset = store.get_dataset(version["dataset_id"]) or {}
    target = Path(out_dir or default_out_dir(dataset.get("name") or "dataset",
                                             version_id, fmt, root or DEFAULT_ROOT))

    if task_id and store.get_task(task_id) is None:
        raise KeyError(f"未知任务 {task_id}：宁可报错，也不要导出一份没人记得的产物")
    tid = task_id or store.create_task("export", dataset.get("id"), version_id, None,
                                       {"format": fmt, **params})
    try:
        report = entry.factory(store, version_id, str(target), **params)
    except Exception as exc:
        store.update_task(tid, "failed", {"format": fmt, "error": str(exc)})
        raise
    report["task_id"] = tid
    report["out_dir"] = str(target)
    if make_scripts:
        report["scripts"] = train_script.generate(target)
    store.update_task(tid, "done", report)
    return report


def export_many(store, version_id: str, formats_: Sequence[str], out_dir=None, root=None,
                **params) -> dict:
    """一次导多种格式：同一批标注，四种读者。"""
    out = {}
    for fmt in formats_:
        dest = None
        if out_dir:
            dest = Path(out_dir) / fmt
        out[fmt] = export(store, version_id, fmt, dest, root=root, **params)
    return out


def validate_export(export_dir, version_id: str, store=None, **kwargs) -> dict:
    """回读校验。包内用这个名字，避免与 `export.validate` 子模块同名混淆。"""
    from .validate import validate

    return validate(export_dir, version_id, store, **kwargs)


def write_scripts(export_dir, **kwargs) -> dict:
    return train_script.generate(export_dir, **kwargs)


__all__ = ["DEFAULT_ROOT", "Dataset", "EXPORTERS", "Sample", "base", "coco",
           "default_out_dir", "export", "export_many", "formats", "load_dataset",
           "multimodal", "read_report", "register", "schema_of", "split", "train_script",
           "validate", "validate_export", "voc", "write_report", "write_scripts", "yolo"]
