"""为导出的数据集生成本地训练脚本与数据卡。

毕设不引 torch/ultralytics（见计划书：完全离线、零显存档位），但导出集必须"可训练"
才算闭环。所以这里生成的是**给使用者自己跑的**脚本：权重只接受本地路径，
脚本里不含任何下载逻辑，缺框架时给出一句明确的话而不是静默失败。
"""

from __future__ import annotations

import json
from pathlib import Path
from string import Template

from .base import read_report

_TRAIN = Template('''#!/usr/bin/env python
"""本数据集的本地训练入口（由 mdva 生成，schema $schema）。

只使用本地文件：权重必须用 --weights 指向磁盘上已有的 .pt，脚本不会联网下载。
缺 ultralytics 时打印安装方法后退出，不做任何隐式获取。
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DATA = "$data"
DEFAULT_TASK = "$task"


def read_manifest():
    report = json.loads((HERE / "dataset_report.json").read_text(encoding="utf-8"))
    return report


def check_local(data_path: Path, weights: str) -> dict:
    problems = []
    if not data_path.is_file():
        problems.append(f"数据描述文件不存在：{data_path}")
    if weights and not Path(weights).is_file():
        problems.append(f"权重文件不存在：{weights}（本脚本不下载权重，请给出本地路径）")
    return {"ok": not problems, "problems": problems}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="本地训练 mdva 导出的 $fmt 数据集")
    ap.add_argument("--data", default=DEFAULT_DATA, help="data.yaml 或 instances.json")
    ap.add_argument("--weights", default="", help="本地预训练权重路径，留空则从头训练")
    ap.add_argument("--task", default=DEFAULT_TASK, choices=["detect", "segment", "classify"])
    ap.add_argument("--device", default="$device", help="cpu 或 0")
    ap.add_argument("--epochs", type=int, default=$epochs)
    ap.add_argument("--imgsz", type=int, default=$imgsz)
    ap.add_argument("--model", default="$model", help="本地 cfg/权重；默认用小网络")
    ap.add_argument("--out", default="runs/train", help="训练输出目录（在数据集之外）")
    ap.add_argument("--dry-run", action="store_true", help="只自检文件与数量，不训练")
    args = ap.parse_args(argv)

    report = read_manifest()
    data_path = (HERE / args.data) if not Path(args.data).is_absolute() else Path(args.data)
    status = check_local(data_path, args.weights)
    print("数据集：", report.get("dataset"), "版本：", report.get("version"))
    print("图片：", report.get("n_images"), "标注：", report.get("n_annotations"),
          "类别：", report.get("n_categories"))
    if not status["ok"]:
        for line in status["problems"]:
            print("[自检失败]", line)
        return 2
    if args.dry_run:
        print("[自检通过] 文件齐备，可以训练。")
        return 0

    try:
        from ultralytics import YOLO
    except ImportError:
        print("本环境未安装 ultralytics（本项目默认依赖里没有它，以保持完全离线）。")
        print("需要训练时：pip install ultralytics 后重跑本脚本，或用 --dry-run 只做自检。")
        return 3

    net = YOLO(args.weights or args.model)
    net.train(data=str(data_path), task=args.task, device=args.device, epochs=args.epochs,
              imgsz=args.imgsz, project=str(HERE / args.out), name="run", exist_ok=True)
    print("训练完成，结果在", HERE / args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
''')

_BAT = """@echo off
rem 本地训练入口（不联网）。先确认 conda 环境已激活。
python "%~dp0train.py" --device cpu --epochs %*
"""


def dataset_card(report: dict) -> str:
    """数据卡：论文附表与 README 直接可用，内容全部来自导出报告，不手写数字。"""
    counts = report.get("counts") or {}
    params = report.get("params") or {}
    lines = [
        f"# {report.get('dataset')} — {report.get('format')} 导出集",
        "",
        f"- 版本：`{report.get('version')}`（父版本 `{report.get('parent')}`，操作 `{report.get('op')}`）",
        f"- 样本：{counts.get('n_samples')} 个（空间 {counts.get('n_spatial')}，"
        f"时序/事件 {counts.get('n_time')}，带掩膜 {counts.get('n_masks')}）",
        f"- 标注：{counts.get('n_annotations')} 条；本次写出 "
        f"{report.get('n_annotations', report.get('spatial_annotations'))} 条，"
        f"格式不支持而跳过 {report.get('n_skipped', 0)} 条",
        f"- 类别：{', '.join(counts.get('categories') or [])}",
        f"- 几何分布：{json.dumps(counts.get('by_geom') or {}, ensure_ascii=False)}",
        f"- 来源分布：{json.dumps(counts.get('by_source') or {}, ensure_ascii=False)}",
        f"- 切分：{json.dumps(report.get('split') or {}, ensure_ascii=False)}",
        f"- 导出参数：{json.dumps(params, ensure_ascii=False)}",
        "",
        "## 怎么读这批数据",
        "",
        "- COCO：`instances.json`（另有 `instances_train/val/test.json`）+ `images/`",
        "- YOLO：`data.yaml` + `images/<fold>/` + `labels/<fold>/`",
        "- VOC：`Annotations/*.xml` + `JPEGImages/` + `ImageSets/Main/*.txt`",
        "- 多模态：`mdva_dataset.json`（无损，含时序/事件/切片/来源留痕）+ `annotations.csv`",
        "",
        "## 训练与校验",
        "",
        "```bash",
        "python train.py --dry-run              # 只做本地文件自检，不训练",
        "python train.py --weights local.pt --device 0",
        "python -m mdva validate <本目录> <版本号>   # 回读校验：结构 + 数量守恒",
        "```",
        "",
        "本目录内不含任何外部 URL；权重与框架需本地已有。",
    ]
    if report.get("issues"):
        lines += ["", "## 导出时记录的坐标问题", ""]
        lines += [f"- {json.dumps(i, ensure_ascii=False)}" for i in report["issues"][:20]]
    return "\n".join(lines) + "\n"


def generate(export_dir, device: str = "cpu", epochs: int = 50, imgsz: int = 640,
             model: str = "yolov8n.yaml", write: bool = True) -> dict:
    """按导出报告生成 train.py / train.bat / DATASET.md。"""
    out = Path(export_dir)
    report = read_report(out)
    fmt = report.get("format")
    task = "detect" if fmt == "yolo" else "segment" if fmt == "coco" else ""
    data = {"yolo": "data.yaml", "coco": "instances_train.json"}.get(fmt, "mdva_dataset.json")
    text = _TRAIN.substitute(schema=1, data=data, task=task or "detect", fmt=fmt,
                             device=device, epochs=int(epochs), imgsz=int(imgsz), model=model)
    files = {}
    if write:
        out.mkdir(parents=True, exist_ok=True)
        (out / "train.py").write_text(text, encoding="utf-8")
        (out / "train.bat").write_text(_BAT, encoding="utf-8")
        (out / "DATASET.md").write_text(dataset_card(report), encoding="utf-8")
        files = {"train.py": str(out / "train.py"), "train.bat": str(out / "train.bat"),
                 "DATASET.md": str(out / "DATASET.md")}
    return {"format": fmt, "data": data, "task": task, **files}


__all__ = ["generate", "dataset_card"]
