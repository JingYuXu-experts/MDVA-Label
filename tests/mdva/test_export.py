"""导出层测试：四种格式 + 回读校验 + 数量守恒。

这里最看重的是"账要平"：写出的 + 明确跳过的 = 库里该导出的条数。
静默丢标注的导出集在训练时看不出来，只有回读对账才抓得到。
"""

import json
import py_compile
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from mdva import datasets as DS
from mdva import export as EXP
from mdva.annotate.model import Ann
from mdva.annotate.store import Store


@pytest.fixture(scope="function")
def env(tmp_path):
    """三种学科的真实文件 + 五种几何 + 时间基准，够覆盖所有导出分支。"""
    root = tmp_path / "data"
    images = root / "cv" / "images"
    masks = root / "cv" / "mask_gt"
    images.mkdir(parents=True)
    masks.mkdir(parents=True)
    scene_paths = []
    for k in range(3):
        rgb, gt, _counts = DS.segmentation_scene(size=96, seed=k)
        p = images / f"scene_{k:03d}.png"
        Image.fromarray(rgb).save(str(p))
        DS._save_labels(masks / f"scene_{k:03d}.png", gt, DS.LABEL_PALETTE)
        scene_paths.append(p)

    stack = root / "payload" / "stack.npy"
    stack.parent.mkdir(parents=True)
    np.save(str(stack), np.linspace(0, 1, 4 * 64 * 64, dtype=np.float32).reshape(4, 64, 64))

    t0 = 1735689600.0
    rows = "\n".join(f"{t0 + i * 60:.3f},{1.0 + 0.1 * i},{2 if i % 7 else 9}" for i in range(30))
    series = root / "telemetry" / "eng.csv"
    series.parent.mkdir(parents=True)
    series.write_text(f"utc,bus_v,mode\n{rows}\n", encoding="utf-8")
    events = root / "telemetry" / "ops.json"
    events.write_text(json.dumps({"events": [
        {"t": t0 + 300, "label": "姿态调整", "level": 1},
        {"t": t0 + 900, "label": "过载", "level": 3, "t_end": t0 + 1200}]}, ensure_ascii=False),
        encoding="utf-8")

    store = Store(tmp_path / "mdva.sqlite")
    did = store.create_dataset("耀斑与病灶", "mixed", root)
    vid = store.put_version(did, None, "init")
    store.set_labels(did, [{"name": "blob", "color": "#DB4D40", "hotkey": "1"},
                           {"name": "stripe", "color": "#33B859", "hotkey": "2"}])

    for p in scene_paths:
        store.put_annotation(vid, str(p), Ann("box", [8, 10, 60, 55], "blob",
                                             source="manual", confidence=1.0))
        store.put_annotation(vid, str(p), Ann("poly", [20, 20, 50, 22, 46, 52, 24, 48], "stripe",
                                             source="visual_prompt", confidence=0.83,
                                             backend="template_match"))
        store.put_annotation(vid, str(p), Ann("point", [70, 70], "blob"))
        store.put_annotation(vid, str(p), Ann("box", [500, 500, 600, 600], "blob"))

    store.put_annotation(vid, str(stack), Ann("box", [4, 6, 30, 40], "blob", slice=1))
    store.put_annotation(vid, str(stack), Ann("box", [10, 10, 50, 50], "stripe", slice=3),
                         state="submitted", role="reviewer")
    store.put_annotation(vid, str(series), Ann("interval", [t0 + 600, t0 + 780], "过温"))
    store.put_annotation(vid, str(events), Ann("level", [t0 + 900, t0 + 1200], "过载", level=3))

    yield type("Env", (), {"root": root, "store": store, "dataset_id": did, "version": vid,
                           "scenes": scene_paths, "stack": stack, "series": series,
                           "events": events, "tmp": tmp_path, "t0": t0})
    store.close()


@pytest.fixture
def out(env):
    d = env.tmp / "exports"
    return str(d)


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------- 注册表

def test_four_formats_are_registered():
    assert EXP.formats() == ["coco", "multimodal", "voc", "yolo"]
    assert "split_ratios" in EXP.schema_of("coco")
    with pytest.raises(KeyError):
        EXP.EXPORTERS.get("tfrecord")


def test_coco_export_is_valid_and_balances(env, tmp_path):
    d = tmp_path / "coco"
    report = EXP.export(env.store, env.version, "coco", str(d))
    data = _load(d / "instances.json")
    assert set(data) >= {"info", "images", "annotations", "categories", "licenses"}
    assert report["n_images"] == 5                     # 3 场景 + 立方体两片
    assert report["n_annotations"] + report["n_skipped"] == report["spatial_annotations"] == 14
    assert len(data["categories"]) == 4 and data["categories"][0]["id"] == 1
    assert [c["name"] for c in data["categories"]] == ["blob", "stripe", "过温", "过载"]
    for a in data["annotations"]:
        img = next(i for i in data["images"] if i["id"] == a["image_id"])
        x, y, w, h = a["bbox"]
        assert 0 <= x and 0 <= y and x + w <= img["width"] + 1e-3 and y + h <= img["height"] + 1e-3
        assert a["area"] > 0 and a["attributes"]["source"] in ("manual", "visual_prompt")
        assert a["mdva"]["annotation_id"].startswith("a")
    assert {i["mdva"]["split"] for i in data["images"]} <= {"train", "val", "test"}
    assert EXP.validate_export(str(d), env.version, env.store)["ok"], \
        EXP.validate_export(str(d), env.version, env.store)


def test_coco_split_files_partition_the_images(env, tmp_path):
    d = tmp_path / "coco"
    report = EXP.export(env.store, env.version, "coco", str(d), split_ratios="0.6/0.2/0.2")
    all_ids = {i["id"] for i in _load(d / "instances.json")["images"]}
    seen: set = set()
    for fold in ("train", "val", "test"):
        ids = {i["id"] for i in _load(d / f"instances_{fold}.json")["images"]}
        assert ids & seen == set()
        seen |= ids
        for a in _load(d / f"instances_{fold}.json")["annotations"]:
            assert a["image_id"] in ids
    assert seen == all_ids
    assert sum(v["images"] for v in report["per_split"].values()) == len(all_ids)


def test_yolo_detect_skips_polygons_and_yolo_segment_keeps_them(env, tmp_path):
    detect = tmp_path / "yolo_detect"
    r1 = EXP.export(env.store, env.version, "yolo", str(detect))
    lines = [ln for txt in detect.rglob("labels/**/*.txt") for ln in txt.read_text().splitlines()]
    assert lines and all(len(l.split()) == 5 for l in lines)
    assert r1["n_skipped"] >= 3                       # 三个 poly 在 detect 任务下被跳过
    for l in lines:
        assert all(0.0 <= float(v) <= 1.0 for v in l.split()[1:])
    assert EXP.validate_export(str(detect), env.version, env.store)["ok"]

    seg = tmp_path / "yolo_seg"
    r2 = EXP.export(env.store, env.version, "yolo", str(seg), task="segment")
    rows = [ln.split() for txt in seg.rglob("labels/**/*.txt") for ln in txt.read_text().splitlines()]
    assert all(len(r) >= 7 and (len(r) - 1) % 2 == 0 for r in rows)
    assert r2["n_annotations"] + r2["n_skipped"] == r2["spatial_annotations"]
    assert EXP.validate_export(str(seg), env.version, env.store)["ok"]


def test_yolo_data_yaml_matches_categories(env, tmp_path):
    d = tmp_path / "yolo"
    EXP.export(env.store, env.version, "yolo", str(d))
    text = (d / "data.yaml").read_text(encoding="utf-8")
    assert re.search(r"^nc: 4$", text, re.M)          # 标签组 2 类 + 只出现在时序/事件里的 2 类
    assert "  0: blob" in text and "  1: stripe" in text
    assert "http" not in text
    assert (d / "images" / "train").is_dir()


def test_only_labels_narrows_both_objects_and_class_list(env, tmp_path):
    d = tmp_path / "blob_only"
    report = EXP.export(env.store, env.version, "yolo", str(d), only_labels=["blob"])
    text = (d / "data.yaml").read_text(encoding="utf-8")
    assert re.search(r"^nc: 2$", text, re.M)          # 标签组仍是两类，但没有一行走 stripe
    rows = [ln.split() for txt in d.rglob("labels/**/*.txt") for ln in txt.read_text().splitlines()]
    assert rows and {r[0] for r in rows} == {"0"}
    assert report["n_skipped"] >= 4                    # stripe 的框与 poly 都不导了
    assert report["n_annotations"] + report["n_skipped"] == report["spatial_annotations"]
    assert EXP.validate_export(str(d), env.version, env.store)["ok"]


def test_voc_xml_is_parseable_and_splits_cover_every_image(env, tmp_path):
    d = tmp_path / "voc"
    report = EXP.export(env.store, env.version, "voc", str(d))
    xmls = sorted((d / "Annotations").glob("*.xml"))
    assert len(xmls) == report["n_images"] == 5
    objects = 0
    for xml in xmls:
        root = ET.parse(str(xml)).getroot()
        size = root.find("size")
        w, h = int(size.findtext("width")), int(size.findtext("height"))
        assert (d / "JPEGImages" / root.findtext("filename")).is_file()
        for obj in root.findall("object"):
            objects += 1
            b = obj.find("bndbox")
            assert 1 <= int(b.findtext("xmin")) <= int(b.findtext("xmax")) <= w
            assert 1 <= int(b.findtext("ymin")) <= int(b.findtext("ymax")) <= h
            assert obj.findtext("attributes/source") in ("manual", "visual_prompt")
        assert root.find("mdva") is not None            # 数据项出处没丢
    assert objects == report["n_annotations"]
    listed = set()
    for fold in ("train", "val", "test"):
        listed |= set((d / "ImageSets" / "Main" / f"{fold}.txt").read_text().split())
    assert listed == {p.stem for p in xmls}
    assert EXP.validate_export(str(d), env.version, env.store)["ok"]


def test_multimodal_export_is_lossless(env, tmp_path):
    d = tmp_path / "mm"
    report = EXP.export(env.store, env.version, "multimodal", str(d))
    data = _load(d / "mdva_dataset.json")
    geoms = {a["geom"] for s in data["samples"] for a in s["annotations"]}
    assert geoms == {"box", "poly", "point", "interval", "level"}
    assert data["schema"] == EXP.multimodal.SCHEMA
    assert data["sources"] == ["manual", "visual_prompt"]
    assert data["backends"] == ["template_match"]

    time_rows = [(s, a) for s in data["samples"] for a in s["annotations"]
                 if a["geom"] in ("interval", "level")]
    assert len(time_rows) == 2
    for sample, row in time_rows:
        assert row["coords_local"] == pytest.approx(
            [c - sample["t_ref"] for c in row["coords"]])
        assert row["state"] in ("draft", "approved")

    series = next(s for s in data["samples"] if s["modality"] == "series")
    assert series["payload"]["channels"] == ["bus_v", "mode"]
    assert series["payload"]["n_samples"] == 30
    event = next(s for s in data["samples"] if s["modality"] == "event")
    assert event["payload"]["n_events"] == 2 and "3" in event["payload"]["level_histogram"]

    csv_lines = (d / "annotations.csv").read_text(encoding="utf-8-sig").splitlines()
    assert len(csv_lines) - 1 == report["n_annotations"] == 16
    assert report["n_images_copied"] == 5
    assert EXP.validate_export(str(d), env.version, env.store)["ok"]


def test_cube_slices_become_separate_samples(env, tmp_path):
    d = tmp_path / "coco"
    EXP.export(env.store, env.version, "coco", str(d))
    data = _load(d / "instances.json")
    stack_images = [i for i in data["images"] if i["mdva"]["modality"] == "video"]
    assert [i["mdva"]["slice"] for i in stack_images] == sorted([1, 3])
    assert all(i["width"] == 64 and i["height"] == 64 for i in stack_images)
    for i in stack_images:
        assert Image.open(str(d / "images" / i["file_name"])).size == (64, 64)
    anns = {a["mdva"]["slice"]: a["bbox"] for a in data["annotations"]
            if a["mdva"]["uri"] == str(env.stack)}
    assert anns[1][2] == 26.0 and anns[3][2] == 40.0        # 各自的框没有被合并


def test_masks_are_paired_and_originals_stay_byte_identical(env, tmp_path):
    src_bytes = {p: p.read_bytes() for p in list(env.root.rglob("*")) if p.is_file()}
    d = env.tmp / "guarded"
    for fmt in ("coco", "yolo", "voc", "multimodal"):
        EXP.export(env.store, env.version, fmt, str(d / fmt), include_masks=True)
    now = {p: p.read_bytes() for p in list(env.root.rglob("*")) if p.is_file()}
    assert set(now) == set(src_bytes)
    for p, blob in src_bytes.items():
        assert now[p] == blob, f"导出动了原始文件 {p}"
    mask = next((d / "coco" / "masks").glob("*_mask.png"))
    ids = set(np.asarray(Image.open(str(mask))).ravel().tolist())
    assert ids <= {0, 1, 2, 3} and ids != {0}
    assert not list((d / "coco" / "images").glob("*_mask.png"))   # 掩膜不混进图片目录
    assert EXP.validate_export(str(d / "coco"), env.version, env.store)["ok"]


def test_only_approved_can_be_exported(env, tmp_path):
    rows = env.store.list_annotations(env.version)
    box = next(r for r in rows if r["ann"].geom == "box" and r["ann"].coords == [8.0, 10.0, 60.0, 55.0])
    env.store.set_state(box["id"], "annotator", "submit")
    env.store.set_state(box["id"], "reviewer", "approve")
    d = env.tmp / "approved_only"
    report = EXP.export(env.store, env.version, "coco", str(d), states=["approved"])
    data = _load(d / "instances.json")
    assert {a["mdva"]["state"] for a in data["annotations"]} == {"approved"}
    assert report["counts"]["n_annotations"] == 1 and report["n_annotations"] == 1
    assert EXP.validate_export(str(d), env.version, env.store)["ok"]


def test_min_confidence_filter_is_reconciled(env, tmp_path):
    d = env.tmp / "conf"
    report = EXP.export(env.store, env.version, "coco", str(d), min_confidence=0.95)
    assert all(a["attributes"]["score"] >= 0.95 for a in _load(d / "instances.json")["annotations"])
    assert report["n_annotations"] + report["n_skipped"] == report["spatial_annotations"]
    assert EXP.validate_export(str(d), env.version, env.store)["ok"]


def test_out_of_bounds_boxes_are_recorded_not_swallowed(env, tmp_path):
    d = env.tmp / "oob"
    report = EXP.export(env.store, env.version, "coco", str(d))
    kinds = [i["kind"] for i in report["issues"]]
    assert kinds == ["box_out_of_bounds"] * 3          # 三张场景各一个越界框
    assert len(report["skipped"]) == 6                 # 越界 3 + 点 3
    assert {s["reason"] for s in report["skipped"]} == {"矩形裁完没有面积", "COCO instances 不表示点"}


def test_split_keeps_slices_of_one_stack_in_the_same_fold(env):
    ds = EXP.load_dataset(env.store, env.version)
    a = EXP.split.plan_split(ds, seed=0)
    b = EXP.split.plan_split(ds, seed=0)
    assert a == b                                     # 同 seed 可重放
    stack_folds = {name for name, fold in a.items() if "stack" in name}
    assert len({a[n] for n in stack_folds}) == 1      # 同一立方体的两片不拆开
    counts = EXP.split.summary(a)["counts"]
    assert sum(counts.values()) == len(a) == len(ds.samples)
    uneven = EXP.split.summary(EXP.split.plan_split(ds, ratios=(1, 0, 0), seed=1))
    assert uneven["counts"].get("val", 0) == 0


def test_split_rejects_bad_ratios(env):
    ds = EXP.load_dataset(env.store, env.version)
    with pytest.raises(ValueError):
        EXP.split.plan_split(ds, ratios=(0.5, 0.5))
    with pytest.raises(ValueError):
        EXP.split.plan_split(ds, ratios=(0, 0, 0))


# ---------------------------------------------------------------------- 校验器

def test_validate_catches_a_tampered_export(env, tmp_path):
    d = tmp_path / "tampered"
    EXP.export(env.store, env.version, "coco", str(d))
    data = _load(d / "instances.json")
    data["annotations"] = data["annotations"][:-2]
    (d / "instances.json").write_text(json.dumps(data), encoding="utf-8")
    result = EXP.validate_export(str(d), env.version, env.store)
    assert not result["ok"]
    assert any("数量不守恒" in m for m in result["issues"])


def test_validate_catches_out_of_range_boxes(env, tmp_path):
    d = tmp_path / "bad"
    EXP.export(env.store, env.version, "yolo", str(d))
    txt = next(d.rglob("labels/**/*.txt"))
    txt.write_text("0 1.7 0.5 0.2 0.2\n", encoding="utf-8")
    result = EXP.validate_export(str(d), env.version, env.store)
    assert not result["ok"] and any("未归一化" in m for m in result["issues"])


def test_validate_refuses_a_foreign_directory(env, tmp_path):
    d = tmp_path / "other"
    EXP.export(env.store, env.version, "voc", str(d))
    with pytest.raises(FileNotFoundError):
        EXP.validate_export(str(tmp_path / "nothing"), env.version, env.store)
    wrong = env.store.put_version(env.dataset_id, env.version, "clean")
    result = EXP.validate_export(str(d), wrong, env.store)
    assert not result["ok"] and any("不是" in m for m in result["issues"])


def test_export_many_writes_four_self_consistent_copies(env, tmp_path):
    d = tmp_path / "many"
    reports = EXP.export_many(env.store, env.version, EXP.formats(), str(d))
    assert set(reports) == set(EXP.formats())
    for fmt, report in reports.items():
        assert Path(report["out_dir"]).is_dir()
        result = EXP.validate_export(str(d / fmt), env.version, env.store)
        assert result["ok"], result["issues"]


# ---------------------------------------------------------------------- 训练脚本

def test_generated_train_script_compiles_and_stays_offline(env, tmp_path):
    d = tmp_path / "coco"
    report = EXP.export(env.store, env.version, "coco", str(d), make_scripts=True)
    train = d / "train.py"
    assert Path(report["scripts"]["train.py"]) == train
    py_compile.compile(str(train), doraise=True)
    text = train.read_text(encoding="utf-8")
    assert "http" not in text and "--weights" in text and "--dry-run" in text
    card = (d / "DATASET.md").read_text(encoding="utf-8")
    assert "耀斑与病灶" in card and str(report["n_annotations"]) in card
    assert (d / "train.bat").is_file()
    for fmt in ("yolo", "multimodal", "voc"):
        dd = tmp_path / fmt
        EXP.export(env.store, env.version, fmt, str(dd), make_scripts=True)
        py_compile.compile(str(dd / "train.py"), doraise=True)
        assert "http" not in (dd / "train.py").read_text(encoding="utf-8")


def test_train_script_dry_run_checks_local_weights(env, tmp_path):
    d = tmp_path / "coco"
    EXP.export(env.store, env.version, "coco", str(d), make_scripts=True)
    ns = {"__name__": "generated_train", "__file__": str(d / "train.py")}
    exec(compile((d / "train.py").read_text(encoding="utf-8"), "train.py", "exec"), ns)
    assert ns["main"](["--dry-run"]) == 0
    assert ns["main"](["--dry-run", "--weights", "no_such.pt"]) == 2
    assert ns["main"](["--dry-run", "--data", "no_such.json"]) == 2


def test_export_is_recorded_as_a_task_and_appears_in_manifest(env, tmp_path):
    d = tmp_path / "coco"
    report = EXP.export(env.store, env.version, "coco", str(d))
    tasks = env.store.list_tasks(env.dataset_id, "export")
    assert len(tasks) == 1 and tasks[0]["state"] == "done"
    assert tasks[0]["id"] == report["task_id"]
    assert tasks[0]["params"]["format"] == "coco"
    manifest = env.store.export_manifest(env.dataset_id, write=False)
    assert manifest["exports"][0]["format"] == "coco"
    with pytest.raises(KeyError):
        EXP.export(env.store, env.version, "yolo", str(env.tmp / "bad"), task_id="nope")
    assert env.store.get_task(report["task_id"])["state"] == "done"


# ---------------------------------------------------------------------- 命令行

@pytest.fixture
def cli_env(env):
    from mdva import cli

    return cli, env


def test_cli_export_lists_formats(cli_env, capsys):
    cli, _env = cli_env
    assert cli.main(["export", "--list"]) == 0
    out = capsys.readouterr().out
    for fmt in EXP.formats():
        assert fmt in out
    assert "--set segmentation" in out.replace("'", "") or "segmentation" in out


def test_cli_export_verify_and_scripts(cli_env, tmp_path, capsys):
    cli, env = cli_env
    d = tmp_path / "cli"
    code = cli.main(["export", "--db", str(env.tmp / "mdva.sqlite"),
                     "--format", "coco,yolo", "--out", str(d),
                     "--split", "0.5/0.25/0.25", "--scripts", "--verify"])
    assert code == 0
    out = capsys.readouterr().out
    assert "耀斑与病灶" in out and "[OK]" in out
    assert (d / "coco" / "train.py").is_file() and (d / "yolo" / "data.yaml").is_file()
    assert EXP.validate_export(str(d / "yolo"), env.version, env.store)["ok"]


def test_cli_export_passes_exporter_params_through_set(cli_env, tmp_path):
    cli, env = cli_env
    d = tmp_path / "set"
    code = cli.main(["export", "--db", str(env.tmp / "mdva.sqlite"), "--format", "yolo",
                     "--out", str(d), "--set", "task=segment",
                     "--set", "include_masks=true", "--set", "seed=7", "--verify"])
    assert code == 0
    rows = [ln.split() for txt in (d / "yolo").rglob("labels/**/*.txt")
            for ln in txt.read_text().splitlines()]
    assert all((len(r) - 1) % 2 == 0 and len(r) >= 7 for r in rows)
    assert list((d / "yolo" / "masks").rglob("*_mask.png"))
    assert cli.main(["export", "--db", str(env.tmp / "mdva.sqlite"), "--set", "oops"]) == 2


def test_cli_export_reports_bad_input(cli_env, tmp_path, capsys):
    cli, env = cli_env
    assert cli.main(["export"]) == 2
    assert "数据库" in capsys.readouterr().out
    assert cli.main(["export", "--db", str(tmp_path / "none.sqlite")]) == 2
    assert cli.main(["export", "--db", str(env.tmp / "mdva.sqlite"), "--format", "tfrecord"]) == 2
    assert "可用" in capsys.readouterr().out
    assert cli.main(["export", "--db", str(env.tmp / "mdva.sqlite"), "--version", "vmissing"]) == 2
    assert "未知版本" in capsys.readouterr().out


def test_cli_validate_returns_nonzero_on_tampering(cli_env, tmp_path, capsys):
    cli, env = cli_env
    d = tmp_path / "cli_v"
    assert cli.main(["export", "--db", str(env.tmp / "mdva.sqlite"), "--format", "coco",
                     "--out", str(d)]) == 0
    coco = d / "coco" / "instances.json"
    data = _load(coco)
    data["annotations"].pop()
    coco.write_text(json.dumps(data), encoding="utf-8")
    assert cli.main(["validate", str(coco.parent), "--db", str(env.tmp / "mdva.sqlite")]) == 1
    assert "数量不守恒" in capsys.readouterr().out
