import json

import pytest

from mdva import cli
from mdva import synth


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    pytest.importorskip("astropy")
    return synth.make_dataset(tmp_path_factory.mktemp("ds"), n_telemetry=3000, seed=11)


def test_scan_lists_every_modality(dataset, capsys):
    assert cli.main(["scan", str(dataset)]) == 0
    out = capsys.readouterr().out
    for kind in ("image", "cube", "series", "event"):
        assert f"[{kind:<6}]" in out


def test_scan_on_empty_dir_returns_nonzero(tmp_path, capsys):
    assert cli.main(["scan", str(tmp_path)]) == 1
    assert "发现" in capsys.readouterr().out


def test_render_fits_writes_png(dataset, tmp_path, capsys):
    out = tmp_path / "img.png"
    code = cli.main(["render", str(dataset / "image_field.fits"), "--out", str(out),
                     "--interval", "zscale", "--stretch", "asinh", "--colormap", "inferno"])
    assert code == 0 and out.exists()
    assert "已写出" in capsys.readouterr().out


def test_render_cube_chooses_slice(dataset, tmp_path):
    a = tmp_path / "s0.png"
    b = tmp_path / "s5.png"
    cli.main(["render", str(dataset / "cube_sim.fits"), "--index", "0", "--out", str(a)])
    cli.main(["render", str(dataset / "cube_sim.fits"), "--index", "5", "--out", str(b)])
    assert a.read_bytes() != b.read_bytes()


def test_render_series_channel(dataset, tmp_path):
    out = tmp_path / "ch.png"
    code = cli.main(["render", str(dataset / "telemetry_sim.csv"), "--modality", "series",
                     "--index", "1", "--width", "600", "--height", "200", "--out", str(out)])
    assert code == 0 and out.exists()


def test_mosaic_over_directory(dataset, tmp_path):
    out = tmp_path / "grid.png"
    assert cli.main(["mosaic", str(dataset), "--rows", "2", "--cols", "2", "--out", str(out)]) == 0
    assert out.exists()


def test_analyze_produces_report_json(dataset, tmp_path):
    outdir = tmp_path / "an"
    code = cli.main(["analyze", str(dataset / "telemetry_sim.csv"),
                     "--analyzers", "dynamic_threshold,correlation", "--out", str(outdir), "--draw"])
    assert code == 0
    report = json.loads((outdir / "report.json").read_text(encoding="utf-8"))
    assert report["modality"] == "series"
    got = {r["analyzer"] for r in report["results"]}
    assert got == {"dynamic_threshold", "correlation"}
    assert (outdir / "telemetry_sim_annotated.png").exists()


def test_analyze_warns_on_unknown_analyzer(dataset, tmp_path, capsys):
    cli.main(["analyze", str(dataset / "telemetry_sim.csv"), "--analyzers", "nope",
              "--out", str(tmp_path / "x")])
    assert "未知算子 nope" in capsys.readouterr().out


def test_timeaxis_finds_items_in_window(dataset, tmp_path, capsys):
    import mdva.io as io

    found = io.scan(dataset)
    t0 = min(i.t_ref for i in found if i.t_ref is not None)
    out = tmp_path / "link.png"
    code = cli.main(["timeaxis", str(dataset), "--t0", str(t0), "--t1", str(t0 + 60),
                     "--cursor", str(t0 + 30), "--out", str(out)])
    assert code == 0
    assert out.exists()
    assert "命中" in capsys.readouterr().out


def test_timeaxis_outside_range_lists_available_times(dataset, tmp_path, capsys):
    code = cli.main(["timeaxis", str(dataset), "--t0", "1", "--t1", "2",
                     "--out", str(tmp_path / "none.png")])
    assert code == 1
    assert "时间窗内没有数据项" in capsys.readouterr().out


def test_parser_rejects_unknown_subcommand():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["frobnicate", "x"])


def test_datasets_lists_categories(capsys):
    assert cli.main(["datasets", "--list"]) == 0
    out = capsys.readouterr().out
    for name in ("aerospace", "medical", "cv_segmentation"):
        assert name in out


def test_datasets_builds_a_category_and_it_scans(tmp_path, capsys):
    from mdva import datasets

    assert cli.main(["datasets", str(tmp_path), "--categories", "cv_segmentation",
                     "--seed", "5"]) == 0
    assert (tmp_path / "cv_segmentation" / "images" / "scene_000.png").is_file()
    assert cli.main(["scan", str(tmp_path / "cv_segmentation")]) == 0
    assert "[image ]" in capsys.readouterr().out
    assert (tmp_path / "manifest.json").is_file()
    # 只生成一类时不该顺手造出别类
    assert not (tmp_path / "aerospace").exists()
    assert datasets.CATEGORIES["cv_segmentation"]["modalities"] == ["image"]


def test_datasets_rejects_unknown_category(tmp_path, capsys):
    assert cli.main(["datasets", str(tmp_path), "--categories", "weather"]) == 2
    assert "未知类别" in capsys.readouterr().out


def test_mosaic_accepts_several_sources(tmp_path, capsys):
    """论文里的对比图是"同一场景在几个目录中的输出"并排，不是一个目录的前 N 张。"""
    from mdva import datasets

    root = tmp_path / "d"
    datasets.build_cv_segmentation(root, seed=1, n_scenes=2)
    base = root / "cv_segmentation"
    scene = "scene_001"
    paths = [base / "images" / f"{scene}.png",
             base / "mask_gt" / f"{scene}.png",
             base / "pred" / "unet" / f"{scene}.png"]
    out = tmp_path / "grid.png"
    assert cli.main(["mosaic", *[str(p) for p in paths],
                     "--rows", "1", "--cols", "3", "--out", str(out)]) == 0
    assert out.exists()
    assert "3 项拼成 1x3" in capsys.readouterr().out


# ---------------------------------------------------------------------- 清洗 / 增强

@pytest.fixture
def label_db(tmp_path):
    """三张 PNG（其中一张是首张的复制）+ 一个已标注的库，喂给 clean/augment 命令行。"""
    import numpy as np
    from PIL import Image

    from mdva.annotate.model import Ann
    from mdva.annotate.store import Store

    def blob(pos=(30, 40), seed=0, size=120):
        rng = np.random.default_rng(seed)
        yy, xx = np.mgrid[0:size, 0:size]
        arr = rng.normal(30.0, 6.0, (size, size))
        arr += 420.0 * np.exp(-((xx - pos[0]) ** 2 + (yy - pos[1]) ** 2) / (2 * 6.0 ** 2))
        arr = (arr - arr.min()) / (arr.max() - arr.min()) * 255.0
        return np.clip(arr, 0, 255).astype(np.uint8)

    # 亮斑位置各不相同，只有 f0_copy 是真正的重复——dedup 按布局判定，位置不同就不算重复
    paths = []
    for k, pos in enumerate(((30, 40), (80, 60), (95, 95))):
        p = tmp_path / f"f{k}.png"
        Image.fromarray(blob(pos=pos, seed=k)).save(str(p))
        paths.append(str(p))
    dup = tmp_path / "f0_copy.png"
    dup.write_bytes((tmp_path / "f0.png").read_bytes())
    paths.append(str(dup))

    s = Store(tmp_path / "mdva.sqlite")
    did = s.create_dataset("命令行", "detection", tmp_path, meta={"category": "aerospace"})
    vid = s.put_version(did, None, "init")
    s.set_labels(did, [{"name": "blob"}])
    for p in paths:
        s.put_annotation(vid, p, Ann("box", [24.0, 34.0, 38.0, 46.0], "blob"),
                         role="annotator", state="submitted")
    yield tmp_path, paths, s, did, vid
    s.close()


def test_clean_lists_its_operators(capsys):
    assert cli.main(["clean", "--list"]) == 0
    out = capsys.readouterr().out
    assert "清洗算子" in out and "content_filter" in out and "content_corr" in out


def test_clean_preview_writes_nothing(label_db, capsys):
    root, paths, s, did, vid = label_db
    code = cli.main(["clean", "--db", str(root / "mdva.sqlite"), "--preview",
                     "--ops", "dedup,blur:threshold=60"])
    assert code == 0
    out = capsys.readouterr().out
    assert "预览" in out and "剔除 1" in out
    assert [v["id"] for v in s.list_versions(did)] == [vid]     # 预览绝不产版本
    assert not (root / "out").exists()


def test_clean_apply_then_rollback(label_db, capsys):
    from pathlib import Path

    root, paths, s, did, vid = label_db
    before = {p: Path(p).stat().st_size for p in paths}
    code = cli.main(["clean", "--db", str(root / "mdva.sqlite"),
                     "--ops", "crop:box=[10,10,110,110];fraction=1.0",
                     "--uris", paths[0], "--out", str(root / "out"),
                     "--write-transformed", "--author", "xjy"])
    assert code == 0
    out = capsys.readouterr().out
    new = [line.split("→ 新版本 ")[1].split("（")[0].strip() for line in out.splitlines()
           if "→ 新版本" in line][0]
    written = root / "out" / "clean" / new / "0000_f0.png"
    assert written.is_file()
    assert {p: Path(p).stat().st_size for p in paths} == before    # 原件一根手指都没碰

    assert s.get_version(new)["op"] == "clean"
    assert len(s.list_annotations(new)) == 4          # 三条没参与的 + 这条映射到裁剪图上

    assert cli.main(["clean", "--db", str(root / "mdva.sqlite"), "--rollback", new]) == 0
    assert not written.exists()
    assert all(Path(p).is_file() for p in paths)


def test_clean_rejects_unknown_operator(label_db, capsys):
    root, _paths, s, did, vid = label_db
    assert cli.main(["clean", "--db", str(root / "mdva.sqlite"), "--ops", "dedup,sharpness"]) == 2
    assert "未知算子" in capsys.readouterr().out


def test_clean_series_channel_needs_calibration(label_db, tmp_path, capsys):
    """时序清洗的离散通道判定要标定表；命令行给 --calibration 就该用上。"""
    root, _paths, s, did, vid = label_db
    t = list(range(40))
    csv_path = tmp_path / "t.csv"
    csv_path.write_text("utc,SA_Mode\n" + "\n".join(f"{i},{i % 3}" for i in range(1, 40)),
                        encoding="utf-8")
    calib = tmp_path / "t.calib.json"
    calib.write_text(json.dumps({"SA_Mode": {"type": "discrete", "values": {"0": "A"}}}),
                     encoding="utf-8")
    code = cli.main(["clean", "--db", str(root / "mdva.sqlite"), "--ops", "series_clean",
                     "--uris", str(csv_path), "--calibration", str(calib), "--preview"])
    assert code == 0
    assert "参与 1" in capsys.readouterr().out


def test_augment_lists_presets_with_reasons(capsys):
    assert cli.main(["augment", "--list"]) == 0
    out = capsys.readouterr().out
    assert "增强算子" in out and "aerospace" in out and "晨昏线" in out


def test_augment_preset_run_replay_and_rollback(label_db, capsys):
    from pathlib import Path

    root, paths, s, did, vid = label_db
    before = {p: Path(p).stat().st_size for p in paths}
    code = cli.main(["augment", "--db", str(root / "mdva.sqlite"), "--preset", "cv_segmentation",
                     "--seed", "3", "--factor", "2", "--uris", paths[0],
                     "--out", str(root / "out")])
    assert code == 0
    out = capsys.readouterr().out
    new = [line.split("→ 新版本 ")[1].split("（")[0] for line in out.splitlines()
           if "→ 新版本" in line][0]
    assert (root / "out" / "aug" / new).is_dir()
    assert s.get_version(new)["op"] == "augment"
    assert len(s.list_annotations(new)) == 6         # 4 条原件继承 + 2 个增强副本
    assert {p: Path(p).stat().st_size for p in paths} == before

    assert cli.main(["augment", "--db", str(root / "mdva.sqlite"), "--replay", new]) == 0
    assert "完全一致" in capsys.readouterr().out

    assert cli.main(["augment", "--db", str(root / "mdva.sqlite"), "--rollback", new]) == 0
    assert not any((root / "out" / "aug" / new).rglob("*.png"))
    assert all(Path(p).is_file() for p in paths)


def test_augment_check_only_warns_and_stops(label_db, capsys):
    root, paths, s, did, vid = label_db
    code = cli.main(["augment", "--db", str(root / "mdva.sqlite"), "--preset", "aerospace",
                     "--ops", "flip:p_vertical=1.0", "--uris", paths[0],
                     "--out", str(root / "out"), "--check-only"])
    assert code == 0
    out = capsys.readouterr().out
    assert "[提醒]" in out and "上下镜像" in out
    assert [v["id"] for v in s.list_versions(did)] == [vid]


def test_augment_needs_preset_or_ops(label_db, capsys):
    root, _paths, s, did, vid = label_db
    assert cli.main(["augment", "--db", str(root / "mdva.sqlite")]) == 2
    assert "无从增强" in capsys.readouterr().out


# ---------------------------------------------------------------------- serve

def test_serve_parser_defaults_to_loopback_only():
    args = cli.build_parser().parse_args(["serve"])
    assert args.func is cli.cmd_serve
    assert args.host == "127.0.0.1" and args.port == 8765
    assert args.allow_remote is False and args.data == "data"


def test_serve_refuses_a_public_bind(tmp_path, capsys):
    """没带 --allow-remote 就不许监听对外地址——而且要在绑定之前就拒绝。"""
    code = cli.main(["serve", "--data", str(tmp_path), "--host", "0.0.0.0", "--port", "0"])
    assert code == 2
    assert "--allow-remote" in capsys.readouterr().out


def test_serve_tells_the_user_to_generate_data_first(tmp_path, capsys):
    code = cli.main(["serve", "--data", str(tmp_path / "还没有这个目录")])
    assert code == 2
    assert "mdva datasets" in capsys.readouterr().out


def test_serve_db_flag_must_point_at_a_real_database(tmp_path, capsys):
    """--db 指到不存在的文件要当场拒绝，不能默默起一个"空工作区"的服务。"""
    from mdva.annotate.store import Store
    from mdva.web.deps import workspace_from_args

    ghost = tmp_path / "mdva.sqlite"
    assert cli.main(["serve", "--db", str(ghost)]) == 2
    assert "数据库不存在" in capsys.readouterr().out

    Store(ghost).close()
    ws = workspace_from_args(db=str(ghost))
    assert ws.root == tmp_path and ws.names() == ["."]
    with pytest.raises(ValueError):
        workspace_from_args(db=str(tmp_path / "改个名字.sqlite"))
