"""命令行入口：无 GUI 完成接入、渲染、分析与导出。

这一层的存在本身就是"平台"论据——同一套引擎既能被界面调用，
也能进批处理脚本和定时任务，不依赖任何窗口系统。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import numpy as np

from . import analysis, io, render
from .core.model import Slice
from .core.timefmt import fmt_time


def _jsonable(obj):
    if isinstance(obj, np.ndarray):
        return {"__ndarray__": list(obj.shape), "dtype": str(obj.dtype)}
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj) if np.isfinite(obj) else None
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, tuple):
        return list(obj)
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list,)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def cmd_scan(args) -> int:
    items = io.scan(args.path, recursive=not args.no_recursive, limit=args.limit)
    if not items:
        print(f"未在 {args.path} 发现可识别的数据项")
        return 1
    t0, t1 = io.common_span(items)
    print(f"共 {len(items)} 项  时间跨度 {fmt_time(t0)} ~ {fmt_time(t1)}\n")
    for it in items:
        unit = it.meta.get("unit") or ""
        extra = f"  unit={unit}" if unit else ""
        span = f"  views={it.n_views}" if it.n_views > 1 else ""
        print(f"  [{it.modality:<6}] {it.id:<44} {str(it.shape):<16} dtype={it.dtype:<8}"
              f" t={fmt_time(it.t_ref)}{span}{extra}")
    return 0


def cmd_render(args) -> int:
    item = io.load_any(args.path, modality=args.modality)
    window = Slice.at_index(args.index) if args.index else Slice.full()
    style = {"interval": args.interval, "stretch": args.stretch, "colormap": args.colormap}
    if args.vmin is not None:
        style["vmin"] = args.vmin
    if args.vmax is not None:
        style["vmax"] = args.vmax

    result = render.render_item(item, size=(args.width, args.height), window=window, style=style)
    rgb = result.composited()
    out = Path(args.out)
    _save(rgb, out)
    print(f"{item.id}: {result.subtitle}")
    print(f"渲染参数 {json.dumps(_jsonable(result.info), ensure_ascii=False)}")
    print(f"已写出 {out}")
    return 0


def cmd_mosaic(args) -> int:
    # 支持多个来源：论文里的对比图通常是"同一场景在几个目录里的输出"并排，
    # 而不是一个目录里的前 N 张
    items = []
    for source in args.paths:
        found = io.scan(source, limit=args.max_items)
        if not found:
            print(f"[warn] {source} 没有可识别的数据项")
        items.extend(found)
    items = items[: args.max_items]
    if not items:
        print("没有可识别的数据项")
        return 1
    if len(args.paths) > 1:
        # 跨目录比同一场景时，五个格子的文件名一模一样，标题必须带上来源目录
        for it in items:
            it.title = f"{Path(it.uri).parent.name}/{it.id}"
    style = {"interval": args.interval, "stretch": args.stretch, "colormap": args.colormap}
    results = []
    for it in items:
        try:
            results.append(render.render_item(it, size=(args.width, args.height), style=style))
        except Exception as exc:
            print(f"[warn] 渲染失败 {it.id}: {exc}")
    rows = args.rows or _auto_rows(len(results))
    cols = args.cols or max(1, -(-len(results) // rows))
    img = render.render_mosaic(results, rows=rows, cols=cols, cell=(args.width, args.height), gap=args.gap)
    out = Path(args.out)
    _save(img, out)
    print(f"{len(results)} 项拼成 {rows}x{cols}，已写出 {out}")
    return 0


def cmd_analyze(args) -> int:
    item = io.load_any(args.path, modality=args.modality)
    names = args.analyzers.split(",") if args.analyzers else analysis.names()
    window = Slice.at_index(args.index) if args.index else Slice.full()

    out = Path(args.out) if args.out else Path(f"{Path(args.path).stem}_analysis")
    out.mkdir(parents=True, exist_ok=True)

    report = {"item": item.id, "modality": item.modality, "meta": _jsonable(
        {k: v for k, v in item.meta.items() if k != "header"}), "results": []}

    style = {"interval": "zscale", "stretch": "linear", "colormap": args.colormap}
    base = render.render_item(item, size=(args.width, args.height), window=window, style=style)
    combined = base

    for name in names:
        name = name.strip()
        if name not in analysis.names():
            print(f"[warn] 未知算子 {name}，可用: {analysis.names()}")
            continue
        inst = analysis.get(name)
        if not inst.handles(item):
            continue
        res = inst.run(item, window)
        report["results"].append({
            "analyzer": name,
            "metrics": _jsonable(res.metrics),
            "n_overlay": res.overlay.count(),
            "flags": _jsonable([{"t": f.t, "channel": f.channel, "label": f.label, "score": f.score}
                                for f in res.flags[:200]]),
        })
        print(res.summary())
        if res.overlay.boxes or res.overlay.points:
            combined = render.RenderResult(
                rgb=base.rgb, title=base.title, subtitle=base.subtitle,
                info=base.info, overlay=combined.overlay.extend(res.overlay)
                if combined.overlay else res.overlay,
            )

    if args.draw:
        _save(combined.composited(), out / f"{Path(item.id).stem}_annotated.png")
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写出 {out / 'report.json'}")
    return 0


def cmd_timeaxis(args) -> int:
    """跨模态时间联动：同一时间窗内渲染图像与遥测，验证 t_ref 对齐是否成立。"""
    items = io.scan(args.path)
    if not items:
        print(f"未在 {args.path} 发现可识别的数据项")
        return 1

    window = Slice.at_time(args.t0, args.t1) if args.t0 is not None else Slice.full()
    inside = io.items_in_window(items, window)
    if not inside:
        print("当前时间窗内没有数据项；下面列出各项时间供选择")
        for it in items:
            print(f"  {it.id:<44} {fmt_time(it.time_span[0])} ~ {fmt_time(it.time_span[1])}")
        return 1

    print(f"时间窗 {fmt_time(window.t0)} ~ {fmt_time(window.t1)} 命中 {len(inside)} 项")
    results = []
    for it in inside:
        style = {"interval": args.interval, "stretch": args.stretch, "colormap": args.colormap}
        try:
            r = render.render_item(it, size=(args.width, args.height), window=window, style=style,
                                   cursor_t=args.cursor)
            print(f"  [{it.modality:<6}] {it.id}  {r.subtitle}")
            results.append(r)
        except Exception as exc:
            print(f"  [skip] {it.id}: {exc}")

    if results:
        cols = len(results)
        img = render.render_mosaic(results, rows=1, cols=cols, cell=(args.width, args.height))
        _save(img, Path(args.out))
        print(f"联动总图已写出 {args.out}（{cols} 路同一时刻）")
    return 0


def cmd_batch(args) -> int:
    from . import pipeline

    if args.config:
        spec = pipeline.load_spec(args.config)
        if args.source:
            spec.source = args.source
        if args.workers:
            spec.workers = args.workers
        if args.out:
            spec.out = args.out
    else:
        if not args.source:
            print("必须给出 --config 或数据目录")
            return 2
        stages = [pipeline.Stage(s.strip()) for s in args.analyzers.split(",") if s.strip()]
        if not stages:
            print("--analyzers 为空，且未提供 --config")
            return 2
        spec = pipeline.PipelineSpec(
            source=args.source, out=args.out or "mdva_batch", stages=stages,
            workers=args.workers, executor=args.executor, limit=args.limit,
            draw=not args.no_draw,
        )

    result = pipeline.run(spec)
    return 0 if result["summary"]["n_ok"] else 1


def cmd_speedup(args) -> int:
    from . import pipeline

    spec = pipeline.load_spec(args.config) if args.config else pipeline.PipelineSpec(
        source=args.source,
        stages=[pipeline.Stage(s.strip()) for s in args.analyzers.split(",") if s.strip()],
        draw=False,
    )
    spec.draw = False
    report = pipeline.measure_speedup(spec, workers=args.workers)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _auto_rows(n: int) -> int:
    import math

    return max(1, int(math.ceil(math.sqrt(n))))


def _save(rgb: np.ndarray, path: Path) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb).save(str(path))


def cmd_datasets(args) -> int:
    from . import datasets

    if args.list:
        for name, spec in datasets.CATEGORIES.items():
            print(f"  {name:<16} {spec['title']}\n  {'':<16} 模态 {','.join(spec['modalities'])}"
                  f"  压点：{spec['stress']}")
        return 0

    cats = [c.strip() for c in args.categories.split(",") if c.strip()] or None
    unknown = [c for c in (cats or []) if c not in datasets.CATEGORIES]
    if unknown:
        print(f"未知类别 {unknown}，可用：{sorted(datasets.CATEGORIES)}")
        return 2

    results = datasets.build_all(args.root, categories=cats, seed=args.seed)
    total = sum(len(r["written"]) for r in results.values())
    print(f"\n已在 {Path(args.root).resolve()} 生成 {len(results)} 类数据，{total} 组产物")
    print("接着可以直接：mdva scan data/aerospace  |  mdva timeaxis data/medical  |  "
          "mdva batch data/cv_segmentation")
    return 0


def _export_targets(args):
    """定位数据集与版本：--db/--data 二选一，版本缺省用最新那一条。"""
    from .annotate.store import Store

    if args.db:
        db = Path(args.db)
    elif args.data:
        db = Path(args.data) / "mdva.sqlite"
    else:
        print("需要用 --db <mdva.sqlite> 或 --data <数据集目录> 指明数据库")
        return None, None
    if not db.is_file():
        print(f"数据库不存在：{db}（先在网页端建数据集，或用 --data 指向含 mdva.sqlite 的目录）")
        return None, None

    store = Store(db)
    datasets = store.list_datasets()
    if args.dataset:
        hit = [d for d in datasets if d["id"] == args.dataset or d["name"] == args.dataset]
        if len(hit) != 1:
            print(f"数据集 {args.dataset!r} 不明确，可用：")
            for d in datasets:
                print(f"  {d['id']}  {d['name']}  ({d['type']})")
            store.close()
            return None, None
        dataset = hit[0]
    elif len(datasets) == 1:
        dataset = datasets[0]
    else:
        print("这里有多个数据集，请用 --dataset 指定：")
        for d in datasets:
            print(f"  {d['id']}  {d['name']}  ({d['type']})")
        store.close()
        return None, None

    versions = store.list_versions(dataset["id"])
    if args.version:
        version = store.get_version(args.version)
        if version is None:
            print(f"未知版本 {args.version}")
            for v in versions:
                print(f"  {v['id']}  {v['op']}  {v['n_annotations']} 条标注")
            store.close()
            return None, None
    elif versions:
        version = versions[-1]
    else:
        print(f"数据集 {dataset['name']} 还没有任何版本")
        store.close()
        return None, None
    return store, (dataset, version)


def _cli_params(args) -> dict:
    """--set k=v 的通用透传：值先按 JSON 解析，解析不了当字符串。"""
    params: dict = {}
    for item in args.set or []:
        if "=" not in item:
            raise ValueError(f"--set 需要写成 key=value，收到 {item!r}")
        key, raw = item.split("=", 1)
        try:
            params[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            params[key.strip()] = raw
    if args.states:
        params["states"] = [s.strip() for s in args.states.split(",") if s.strip()]
    if args.only_labels:
        params["only_labels"] = [s.strip() for s in args.only_labels.split(",") if s.strip()]
    if args.min_confidence is not None:
        params["min_confidence"] = args.min_confidence
    if args.split:
        params["split_ratios"] = args.split
    if args.seed is not None:
        params["seed"] = args.seed
    return params


def cmd_export(args) -> int:
    from . import export as EXP
    from .export.validate import summarize

    if args.list:
        for name in EXP.formats():
            entry = EXP.EXPORTERS.get(name)
            print(f"  {name:<12} {entry.description}")
            for key, spec in entry.schema.items():
                default = spec.get("default")
                print(f"      --set {key}=…  默认 {json.dumps(default, ensure_ascii=False)}"
                      f"  {spec.get('description', '')}")
        return 0

    store, found = _export_targets(args)
    if store is None:
        return 2
    try:
        dataset, version = found
        formats = EXP.formats() if args.format in ("all", "全部") else [
            f.strip() for f in args.format.split(",") if f.strip()]
        unknown = [f for f in formats if f not in EXP.EXPORTERS]
        if unknown:
            print(f"未知格式 {unknown}，可用：{EXP.formats()} 或 all")
            return 2
        try:
            params = _cli_params(args)
        except ValueError as exc:
            print(f"参数写法有误：{exc}")
            return 2
        print(f"数据集 {dataset['name']}（{dataset['id']}） 版本 {version['id']}"
              f"（{version['op']}，{version['n_annotations']} 条标注）")
        for fmt in formats:
            out_dir = str(Path(args.out) / fmt) if args.out else None
            report = EXP.export(store, version["id"], fmt, out_dir,
                               make_scripts=args.scripts, root=args.out_root, **params)
            print(f"  [{fmt}] {report['out_dir']}")
            print(f"      图片 {report.get('n_images')}  标注 {report.get('n_annotations')}"
                  f"  跳过 {report.get('n_skipped', 0)}  "
                  f"切分 {report.get('split', {}).get('counts')}")
            if report.get("issues"):
                print(f"      坐标问题 {len(report['issues'])} 处，详见 dataset_report.json")
            if args.verify:
                check = EXP.validate_export(report["out_dir"], version["id"], store)
                print("      " + summarize(check))
                if not check["ok"]:
                    return 1
        return 0
    finally:
        store.close()


def cmd_validate(args) -> int:
    from . import export as EXP
    from .export.validate import summarize

    store, found = _export_targets(args)
    if store is None:
        return 2
    try:
        version_id = found[1]["id"]
        result = EXP.validate_export(args.dir, version_id, store)
        print(summarize(result))
        for line in result["issues"]:
            print("  -", line)
        return 0 if result["ok"] else 1
    finally:
        store.close()


def _split_top(text: str, sep: str) -> list:
    """按 sep 切分，但忽略 [] {} 与引号内部的分隔符。

    `--ops "crop:box=[10,10,110,110],blur"` 里那个数组自带逗号：不加这道判断，
    一个算子会被劈成三段，报"未知算子 ['10', '110', …]"——那是命令行最难查的一类错。
    """
    parts, buf, depth, quote = [], [], 0, ""
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{(":
            depth += 1
        elif ch in "]})":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return parts


def _parse_ops(text: str) -> list:
    """`dedup:distance=4;keep=last,crop:box=[10,10,110,110]` —— 逗号分算子，分号分参数。

    算子参数用分号而不是逗号分隔，是为了让数组值（box=[…]）能直接写在命令行上。
    """
    specs = []
    for chunk in (s.strip() for s in _split_top(text or "", ",")):
        if not chunk:
            continue
        name, _, tail = chunk.partition(":")
        params = {}
        for pair in (p.strip() for p in _split_top(tail, ";")):
            if not pair:
                continue
            if "=" not in pair:
                raise ValueError(f"算子参数要写成 key=value，收到 {pair!r}")
            key, raw = pair.split("=", 1)
            try:
                params[key.strip()] = json.loads(raw)
            except json.JSONDecodeError:
                params[key.strip()] = raw
        specs.append({"name": name.strip(), "params": params})
    return specs


def _op_specs(args, registry) -> list:
    """`--config` 文件优先，其次命令行 `--ops`。文件里可以写 content_filter 的 prompts。"""
    if getattr(args, "config", ""):
        obj = json.loads(Path(args.config).read_text(encoding="utf-8"))
        specs = obj.get("ops") if isinstance(obj, dict) else obj
    else:
        specs = _parse_ops(getattr(args, "ops", ""))
    unknown = [s.get("name") for s in specs if s.get("name") not in registry]
    if unknown:
        print(f"未知算子 {unknown}，可用：{registry.names()}")
        return []
    return specs


def _calibration(args):
    if not getattr(args, "calibration", ""):
        return None
    from .io.series import Calibration

    return Calibration.load(Path(args.calibration))


def _print_ops(registry, title: str) -> None:
    print(f"{title}（{len(registry.names())} 个）：")
    for name in registry.names():
        entry = registry.get(name)
        print(f"  {name:<16} {entry.description}")
        for key, spec in entry.schema.items():
            print(f"      {key}=…  默认 {json.dumps(spec.get('default'), ensure_ascii=False)}"
                  f"  {spec.get('description', '')}")


def cmd_clean(args) -> int:
    from . import clean as CL

    if args.list:
        _print_ops(CL.CLEAN_OPS, "清洗算子")
        return 0
    store, found = _export_targets(args)
    if store is None:
        return 2
    try:
        if args.rollback:
            result = CL.rollback(store, args.rollback)
            print(f"已回退 {args.rollback}：以父版本 {result['restored']} 产出 "
                  f"{result['new_version']}")
            print(f"  删掉我们自己写出去的文件 {len(result['removed_files'])} 个，"
                  f"跳过 {len(result['skipped_files'])} 个（字节数不是我们记的那些就不动）")
            print(f"  被回退版本仍有 {result['annotations_kept']} 条标注——清洗从不删标注")
            return 0

        dataset, version = found
        specs = _op_specs(args, CL.CLEAN_OPS)
        if not specs:
            print("没有指定算子：用 --ops dedup,blur 或 --config clean.json（--list 看全部）")
            return 2
        uris = [u.strip() for u in args.uris.split(",") if u.strip()] or \
            sorted(store.annotated_items(version["id"]))
        items = CL.load_items(uris)
        calib = _calibration(args)

        if args.preview:
            summary = CL.preview(items, specs, calib)
            print(f"[预览，不改任何东西] 共 {summary['n_items']} 条：保留 {summary['n_kept']}，"
                  f"剔除 {summary['n_dropped']}，变换 {summary['n_transforms']}")
            for op_name, stat in summary["per_op"].items():
                print(f"  {op_name:<16} 参与 {stat['n']}，剔除 {stat['dropped']}，"
                      f"变换 {stat['transformed']}")
            print("去掉 --preview 才会产新版本；原件在任何时候都不会被动。")
            return 0

        report = CL.apply(store, version["id"], specs,
                         uris=[str(i.uri) for i in items], out_dir=args.out or None,
                         calibration=calib, write_transformed=args.write_transformed,
                         author=args.author)
        print(f"数据集 {dataset['name']} 版本 {version['id']} → 新版本 {report['version']}")
        print(f"  保留 {len(report['kept'])} 条，剔除 {len(report['dropped'])} 条，"
              f"变换 {len(report['transforms'])} 条")
        for row in report["dropped"][:20]:
            print(f"    剔除 {Path(row['uri']).name}  [{row['op']}] {row['reason']}")
        for group in report["groups"][:10]:
            print(f"    重复组：留 {Path(group['keep']).name}，同内容 {len(group['drop'])} 条")
        if report["written"]:
            print(f"  变换结果写到 {Path(report['written'][0]['written']).parent}")
        if args.rollback or report["written"]:
            print(f"  一键回退：mdva clean --db {args.db or Path(args.data) / 'mdva.sqlite'}"
                  f" --rollback {report['version']}")
        return 0
    finally:
        store.close()


def cmd_augment(args) -> int:
    from . import augment as AUG
    from . import clean as CL

    if args.list:
        _print_ops(AUG.AUG_OPS, "增强算子")
        print("\n学科预设（--preset 选一个，算子与参数都照它来）：")
        for preset in AUG.presets():
            ops = "/".join(o["name"] for o in preset["recipe"]["ops"])
            print(f"  {preset['name']:<16} factor={preset['recipe']['factor']}  {ops}")
            print(f"      {preset['reason']}")
        return 0

    store, found = _export_targets(args)
    if store is None:
        return 2
    try:
        if args.rollback:
            result = CL.rollback(store, args.rollback)
            print(f"已回退 {args.rollback}（{result['op']}）：删掉自己写的文件 "
                  f"{len(result['removed_files'])} 个，跳过 {len(result['skipped_files'])} 个")
            return 0
        if args.replay:
            out = AUG.replay(store, args.replay)
            verdict = "与原谱系完全一致" if out["identical"] else \
                f"{len(out['diff'])} 处不一致"
            print(f"重放 {args.replay}：{out['n_jobs']} 条计划，{verdict}")
            for row in out["diff"][:10]:
                print(f"  - {Path(row['uri']).name} 副本 {row['rep']}：{row['why']}")
            return 0 if out["identical"] else 1

        dataset, version = found
        recipe_obj = dict(AUG.get(args.preset)["recipe"]) if args.preset else {}
        for key, value in (("seed", args.seed), ("factor", args.factor),
                           ("limit", args.limit), ("index", args.index)):
            if value is not None:
                recipe_obj[key] = value
        if recipe_obj.get("seed") is None:
            recipe_obj["seed"] = 0
        specs = _op_specs(args, AUG.AUG_OPS)
        if specs:
            recipe_obj["ops"] = specs
        if not recipe_obj.get("ops"):
            print("既没给 --preset 也没给 --ops，无从增强。先看 --list。")
            return 2
        try:
            recipe = AUG.Recipe.from_obj(recipe_obj)
        except (ValueError, TypeError) as exc:
            print(f"配方有问题：{exc}")
            return 2

        uris = [u.strip() for u in args.uris.split(",") if u.strip()] or \
            sorted(store.annotated_items(version["id"]))
        items = CL.load_items(uris)
        for line in AUG.check(recipe, items,
                              category=str((dataset.get("meta") or {}).get("category", ""))):
            print(f"  [提醒] {line}")
        if args.check_only:
            print("只做了检查，没有产版本。去掉 --check-only 才会真跑。")
            return 0

        report = AUG.run(store, version["id"], recipe, out_dir=args.out or None,
                         uris=[str(i.uri) for i in items], author=args.author)
        print(f"数据集 {dataset['name']} 版本 {version['id']} → 新版本 {report['version']}"
              f"（seed {recipe.seed}，factor {recipe.factor}）")
        print(f"  {report['summary']}")
        print(f"  产物目录 {report['out_dir']}")
        print(f"  重放校验：mdva augment --replay {report['version']} …")
        return 0
    finally:
        store.close()


def cmd_bench(args) -> int:
    from . import backends as BE

    root = Path(args.root)
    if not root.is_dir():
        print(f"数据目录不存在：{root}（可先运行 mdva datasets {root}）")
        return 2

    cats = [c.strip() for c in args.categories.split(",") if c.strip()] or None
    only = [o.strip() for o in args.only.split(",") if o.strip()] or None
    table_path = Path(args.out)
    if args.reuse and table_path.is_file():
        table = BE.load_table(table_path)
        print(f"复用已有实测表 {table_path}（{len(table)} 行）；去掉 --reuse 才会重跑")
    else:
        print(f"评测 {root} …（{only or '全部后端'}，每模态取 {args.limit} 路）")
        rows = BE.bench_run(root, categories=cats, only=only, limit=args.limit,
                            include_stubs=args.stubs)
        if not rows:
            print("没有产生任何评测行：检查 truth.json 是否存在、后端与模态是否匹配")
            return 1
        table = [r.to_json() for r in rows]
        BE.bench_save(rows, table_path)
        print(f"实测表写入 {table_path}（{len(rows)} 行）\n")
        print(f"{'后端':<18}{'任务':<16}{'学科':<12}{'F1':>6}{'P':>6}{'R':>6}{'延迟ms':>9}  判据")
        for row in table:
            print(f"{row['backend']:<18}{row['task']:<16}{row['category']:<12}"
                  f"{row['f1']:>6.2f}{row['precision']:>6.2f}{row['recall']:>6.2f}"
                  f"{row['latency_ms']:>9.1f}  {row['kind']}｜{row['note']}")

    if args.recommend:
        probe = BE.detect()
        print(f"\n推荐（{args.domain or '通用'} / {args.task or '默认任务'}，"
              f"档位 {probe.tier()}）：")
        for i, item in enumerate(BE.top3(args.domain, args.task, probe, table,
                                         k=int(args.recommend)), 1):
            print(f"  {i}. {item['title']}  综合分 {item['score']:.3f}")
            for reason in item["reasons"]:
                print(f"       - {reason}")
    if args.check:
        ok, _f1, why = BE.usable(args.check, args.task, args.domain, args.threshold, table)
        print(f"\n门槛检查 {args.check}：{'可用' if ok else '禁用'} —— {why}")
        return 0 if ok else 1
    return 0


def cmd_serve(args) -> int:
    """起本地标注平台：只绑回环，浏览器打开即是全部界面。"""
    from .web.deps import workspace_from_args
    from .web.server import serve

    try:
        ws = workspace_from_args(db=args.db, data=args.data, out=args.out)
    except (FileNotFoundError, ValueError) as exc:
        print(f"{exc}")
        return 2
    if not ws.names():
        root = Path(args.db).parent if args.db else Path(args.data)
        print(f"目录不存在：{root.resolve()}")
        print(f"先生成示例数据再启动：mdva datasets {args.data} && mdva serve --data {args.data}")
        return 2
    return serve(ws, host=args.host, port=args.port, static_dir=args.static,
                 verbose=args.verbose, open_browser=args.open,
                 allow_remote=args.allow_remote)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mdva", description="多模态科研数据可视化与并行分析核心引擎")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="枚举并探测数据项（只读头，不读数据）")
    p.add_argument("path")
    p.add_argument("--no-recursive", action="store_true")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_scan)

    def common(sp):
        sp.add_argument("--modality", default=None,
                        choices=["image", "cube", "series", "event", "video"])
        sp.add_argument("--index", type=int, default=0, help="立方体切片序号")
        sp.add_argument("--width", type=int, default=760)
        sp.add_argument("--height", type=int, default=420)
        sp.add_argument("--interval", default="zscale")
        sp.add_argument("--stretch", default="linear")
        sp.add_argument("--colormap", default="gray")
        sp.add_argument("--out", required=True)

    p = sub.add_parser("render", help="渲染单个数据项")
    p.add_argument("path")
    p.add_argument("--vmin", type=float, default=None)
    p.add_argument("--vmax", type=float, default=None)
    common(p)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("mosaic", help="批量并行对比拼图，可跨目录取同一场景的多个输出")
    p.add_argument("paths", nargs="+", help="目录或文件，按给定顺序拼接")
    p.add_argument("--rows", type=int, default=0)
    p.add_argument("--cols", type=int, default=0)
    p.add_argument("--gap", type=int, default=4)
    p.add_argument("--max-items", type=int, default=64)
    common(p)
    p.set_defaults(func=cmd_mosaic)

    p = sub.add_parser("analyze", help="跑算子并导出标注图与 JSON 报告")
    p.add_argument("path")
    p.add_argument("--analyzers", default="", help="逗号分隔，缺省跑全部")
    p.add_argument("--draw", action="store_true", help="叠加标注并出图")
    p.add_argument("--index", type=int, default=0)
    p.add_argument("--modality", default=None)
    p.add_argument("--width", type=int, default=760)
    p.add_argument("--height", type=int, default=420)
    p.add_argument("--colormap", default="gray")
    p.add_argument("--out", default="")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("timeaxis", help="跨模态时间联动：同一时间窗渲染多路数据")
    p.add_argument("path")
    p.add_argument("--t0", type=float, default=None)
    p.add_argument("--t1", type=float, default=None)
    p.add_argument("--cursor", type=float, default=None)
    p.add_argument("--interval", default="zscale")
    p.add_argument("--stretch", default="linear")
    p.add_argument("--colormap", default="gray")
    p.add_argument("--width", type=int, default=560)
    p.add_argument("--height", type=int, default=320)
    p.add_argument("--out", default="timeaxis.png")
    p.set_defaults(func=cmd_timeaxis)

    p = sub.add_parser("batch", help="配置驱动的并行批量分析，导出指标表与总览图")
    p.add_argument("source", nargs="?", default="", help="数据目录；用 --config 时可省略")
    p.add_argument("--config", default="", help="JSON/YAML 流水线配置")
    p.add_argument("--analyzers", default="", help="逗号分隔算子名，缺省跑全部")
    p.add_argument("--out", default="")
    p.add_argument("--workers", type=int, default=0, help="0 表示按 CPU 与项数自动取")
    p.add_argument("--executor", default="thread", choices=["thread", "process"])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--no-draw", action="store_true")
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("speedup", help="测并行加速比，供论文实验章节使用")
    p.add_argument("source", nargs="?", default="", help="数据目录；用 --config 时可省略")
    p.add_argument("--config", default="")
    p.add_argument("--analyzers", default="bg_stats,image_quality")
    p.add_argument("--workers", type=int, default=0)
    p.set_defaults(func=cmd_speedup)

    p = sub.add_parser("datasets", help="生成分学科示例数据集（航空航天/医学/图像分割）")
    p.add_argument("root", nargs="?", default="data")
    p.add_argument("--categories", default="", help="逗号分隔，缺省生成全部")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--list", action="store_true", help="只列出类别与其压测的能力")
    p.set_defaults(func=cmd_datasets)

    def locator(sp):
        sp.add_argument("--db", default="", help="mdva.sqlite 路径")
        sp.add_argument("--data", default="", help="数据集目录（内含 mdva.sqlite）")
        sp.add_argument("--dataset", default="", help="数据集 id 或名称，只有一个时可省略")
        sp.add_argument("--version", default="", help="版本 id，缺省用最新一条")

    p = sub.add_parser("export", help="把标注版本导出为 COCO/YOLO/VOC/多模态 JSON")
    p.add_argument("--format", default="coco", help="coco/yolo/voc/multimodal 或逗号分隔、all")
    p.add_argument("--out", default="", help="导出目录（每种格式各占一个子目录）")
    p.add_argument("--out-root", default="", help="未指定 --out 时的根目录，默认 ./out")
    p.add_argument("--states", default="", help="逗号分隔的审核状态白名单，如 approved,submitted")
    p.add_argument("--only-labels", default="", help="逗号分隔的类别白名单")
    p.add_argument("--min-confidence", type=float, default=None)
    p.add_argument("--split", default="", help="train/val/test 比例，如 0.7/0.15/0.15")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--set", action="append", metavar="KEY=VALUE",
                   help="导出器参数透传，如 --set task=segment --set include_masks=true")
    p.add_argument("--scripts", action="store_true", help="顺带生成 train.py / DATASET.md")
    p.add_argument("--verify", action="store_true", help="导完立即回读校验")
    p.add_argument("--list", action="store_true", help="列出所有格式及其可调参数")
    locator(p)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("validate", help="回读校验导出的数据集与数据库版本是否一致")
    p.add_argument("dir", help="导出目录")
    locator(p)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("clean", help="数据清洗：去相似/去模糊/内容过滤/时序体检，只产新版本不删原件")
    p.add_argument("--ops", default="",
                   help="逗号分隔算子，算子参数用分号：dedup:distance=4;keep=last,blur")
    p.add_argument("--config", default="", help="JSON 配置（content_filter 要带 prompts 时用这个）")
    p.add_argument("--uris", default="", help="逗号分隔条目路径，缺省用该版本已标注的全部条目")
    p.add_argument("--out", default="", help="变换结果与写盘清单的根目录，默认 ./out")
    p.add_argument("--calibration", default="", help="标定表，series_clean 判离散通道要用")
    p.add_argument("--preview", action="store_true", help="只算不写：报告各算子会剔除几条")
    p.add_argument("--write-transformed", action="store_true", dest="write_transformed",
                   help="把 crop/rotate/mirror 的结果写成新文件（默认只产版本）")
    p.add_argument("--rollback", default="", metavar="版本id", help="一键回退这个清洗版本")
    p.add_argument("--author", default="")
    p.add_argument("--list", action="store_true", help="列出算子与参数")
    locator(p)
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("augment", help="数据增强：配方 + seed 谱系，可逐字节重放")
    p.add_argument("--preset", default="", help="aerospace / medical / cv_segmentation / series")
    p.add_argument("--ops", default="", help="覆盖预设的算子串，写法同 clean")
    p.add_argument("--config", default="")
    p.add_argument("--uris", default="")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--factor", type=int, default=None, help="每张原图产几个副本")
    p.add_argument("--limit", type=int, default=None, help="最多增强几个源条目")
    p.add_argument("--index", type=int, default=None, help="立方体/视频取第几帧")
    p.add_argument("--out", default="")
    p.add_argument("--author", default="")
    p.add_argument("--check-only", action="store_true", dest="check_only",
                   help="只跑学科策略检查，不产版本")
    p.add_argument("--replay", default="", metavar="版本id", help="按存下的配方重放谱系并比对")
    p.add_argument("--rollback", default="", metavar="版本id")
    p.add_argument("--list", action="store_true", help="列出算子、参数与学科预设")
    locator(p)
    p.set_defaults(func=cmd_augment)

    p = sub.add_parser("bench", help="后端实测：精度/延迟表 + 推荐排序 + 指定后端准入门槛")
    p.add_argument("root", nargs="?", default="data", help="数据集目录（含 truth.json）")
    p.add_argument("--categories", default="", help="逗号分隔的学科目录名")
    p.add_argument("--only", default="", help="逗号分隔的后端名，缺省测全部可用后端")
    p.add_argument("--limit", type=int, default=4, help="每个模态最多取几路参与评测")
    p.add_argument("--out", default=str(Path("out") / "bench" / "backends.json"))
    p.add_argument("--reuse", action="store_true", help="复用已有实测表，不重跑")
    p.add_argument("--stubs", action="store_true", help="把深度学习占位后端也列出来")
    p.add_argument("--recommend", type=int, default=0, metavar="N", help="测完打印前 N 个推荐")
    p.add_argument("--check", default="", metavar="后端名", help="检查该后端是否达到门槛")
    p.add_argument("--domain", default="", help="学科/领域，如 aerospace")
    p.add_argument("--task", default="", help="任务，如 series_anomaly")
    p.add_argument("--threshold", type=float, default=0.90, help="准入门槛，默认 0.90")
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("serve", help="启动本地标注平台（只绑 127.0.0.1，数据不出机器）")
    p.add_argument("--data", default="data", help="工作区根目录（可含分学科子目录）")
    p.add_argument("--db", default="", help="直接指定某个 mdva.sqlite（其父目录即工作区）")
    p.add_argument("--out", default="", help="清洗/增强/导出的产物根目录，默认各工作区自己的 out/")
    p.add_argument("--host", default="127.0.0.1", help="监听地址，非回环需要 --allow-remote")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--static", default="", help="前端构建产物目录，默认 mdva/web/static")
    p.add_argument("--open", action="store_true", help="启动后用系统浏览器打开")
    p.add_argument("--verbose", action="store_true", help="打印每一条请求日志")
    p.add_argument("--allow-remote", action="store_true",
                   help="允许监听非回环地址（会把整个数据集暴露到局域网络，慎用）")
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: Optional[list] = None) -> int:
    # Windows 控制台默认 GBK，中文输出会乱码；先固定为 UTF-8 再解析参数
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
