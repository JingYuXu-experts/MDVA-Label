import json
from pathlib import Path

import pytest

from mdva import pipeline
from mdva.pipeline import PipelineSpec, Stage


@pytest.fixture()
def dataset(tmp_path):
    pytest.importorskip("astropy")
    from mdva import synth

    return synth.make_dataset(tmp_path / "ds", n_telemetry=1200, seed=21)


def _spec(root, stages, out, **kw):
    return PipelineSpec(source=str(root), out=str(out), stages=stages, **kw)


def test_stage_from_string_and_dict():
    assert Stage.from_obj("bg_stats") == Stage("bg_stats", {})
    s = Stage.from_obj({"analyzer": "star_detect", "params": {"nsigma": 9}})
    assert s.analyzer == "star_detect" and s.params["nsigma"] == 9
    inline = Stage.from_obj({"name": "dynamic_threshold", "window": 50})
    assert inline.analyzer == "dynamic_threshold" and inline.params["window"] == 50


def test_stage_from_obj_rejects_bad_input():
    with pytest.raises(ValueError):
        Stage.from_obj({"params": {}})
    with pytest.raises(TypeError):
        Stage.from_obj(42)


def test_spec_window_becomes_slice():
    spec = PipelineSpec(source="x", window=(100.0, 200.0))
    sl = spec.slice
    assert sl.t0 == 100.0 and sl.t1 == 200.0
    assert PipelineSpec(source="x").slice.t0 == float("-inf")


def test_spec_from_obj_drops_unknown_keys_with_warning(capsys):
    spec = PipelineSpec.from_obj({"source": "d", "stages": ["bg_stats"], "bogus": 1})
    assert spec.source == "d" and len(spec.stages) == 1
    assert "bogus" in capsys.readouterr().out


def test_load_spec_from_json(tmp_path):
    cfg = tmp_path / "p.json"
    cfg.write_text(json.dumps({
        "source": "some/dir",
        "out": "out",
        "workers": 2,
        "stages": ["bg_stats", {"analyzer": "image_quality"}],
    }), encoding="utf-8")
    spec = pipeline.load_spec(cfg)
    assert spec.workers == 2
    assert [s.analyzer for s in spec.stages] == ["bg_stats", "image_quality"]


def test_load_spec_yaml_requires_pyyaml(tmp_path):
    cfg = tmp_path / "p.yaml"
    cfg.write_text("source: some/dir\nstages:\n  - bg_stats\n", encoding="utf-8")
    try:
        import yaml  # noqa: F401
    except ImportError:
        with pytest.raises(ImportError):
            pipeline.load_spec(cfg)
    else:
        assert pipeline.load_spec(cfg).stages[0].analyzer == "bg_stats"


def test_run_serial_produces_metrics(dataset, tmp_path):
    spec = _spec(dataset, [Stage("bg_stats")], tmp_path / "o1", draw=False, workers=1)
    result = pipeline.run(spec, progress=False)
    s = result["summary"]
    assert s["n_items"] == 4 and s["n_ok"] == 4 and s["n_failed"] == 0
    assert s["executor"] == "serial"
    image_rows = [r for r in result["records"] if r["modality"] in ("image", "cube")]
    assert image_rows and all("bg_stats.bg_median" in r["metrics"] for r in image_rows)


def test_series_stages_are_skipped_on_images(dataset, tmp_path):
    spec = _spec(dataset, [Stage("dynamic_threshold")], tmp_path / "o2", draw=False, workers=1)
    result = pipeline.run(spec, progress=False)
    series = [r for r in result["records"] if r["modality"] == "series"]
    others = [r for r in result["records"] if r["modality"] != "series"]
    assert series and others
    assert all(r["metrics"] == {} for r in others), "非时序模态不应产生时序算子指标"
    assert all("dynamic_threshold.n_events" in r["metrics"] for r in series)


def test_unknown_analyzer_marks_record_failed_without_aborting(dataset, tmp_path):
    spec = _spec(dataset, [Stage("no_such_analyzer")], tmp_path / "o3", draw=False, workers=1)
    result = pipeline.run(spec, progress=False)
    assert result["summary"]["n_ok"] == 0
    assert all("未知算子" in r["error"] for r in result["records"])


def test_broken_file_is_skipped_without_aborting_the_batch(tmp_path, dataset):
    """坏文件在 scan 阶段就被剔除，其余照常跑完——例行数据处理的基本底线。"""
    import shutil

    root = tmp_path / "mixed"
    root.mkdir()
    for p in dataset.iterdir():
        shutil.copy(str(p), str(root / p.name))
    (root / "broken.fits").write_bytes(b"NOT A FITS FILE")

    spec = _spec(root, [Stage("bg_stats")], tmp_path / "o4", draw=False, workers=1)
    result = pipeline.run(spec, progress=False)
    assert result["summary"]["n_items"] == 4, "损坏的 fits 应被 scan 过滤掉"
    assert result["summary"]["n_ok"] == 4
    assert result["summary"]["n_failed"] == 0


def test_thread_pool_matches_serial_results(dataset, tmp_path):
    stages = [Stage("bg_stats"), Stage("image_quality"), Stage("star_detect", {"nsigma": 6})]
    a = pipeline.run(_spec(dataset, stages, tmp_path / "s", draw=False, workers=1), progress=False)
    b = pipeline.run(_spec(dataset, stages, tmp_path / "p", draw=False, workers=4,
                           executor="thread"), progress=False)
    assert pipeline._records_match(a["records"], b["records"])
    assert b["summary"]["workers"] > 1


def test_process_pool_returns_same_metrics(dataset, tmp_path):
    stages = [Stage("bg_stats")]
    a = pipeline.run(_spec(dataset, stages, tmp_path / "ps", draw=False, workers=1), progress=False)
    b = pipeline.run(_spec(dataset, stages, tmp_path / "pp", draw=False, workers=2,
                           executor="process"), progress=False)
    assert pipeline._records_match(a["records"], b["records"])


def test_reports_and_overview_are_written(dataset, tmp_path):
    out = tmp_path / "art"
    spec = _spec(dataset, [Stage("bg_stats")], out, draw=True, workers=1)
    pipeline.run(spec, progress=False)
    assert (out / "batch_report.json").exists()
    assert (out / "metrics.csv").exists()
    assert (out / "annotated" / "overview.png").exists()

    payload = json.loads((out / "batch_report.json").read_text(encoding="utf-8"))
    assert payload["summary"]["n_ok"] == 4
    assert len(payload["records"]) == 4

    header = (out / "metrics.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("item,modality,ok,elapsed_s")
    assert "bg_stats.bg_median" in header


def test_metrics_csv_has_no_bare_commas_in_cells(dataset, tmp_path):
    out = tmp_path / "csv"
    pipeline.run(_spec(dataset, [Stage("bg_stats")], out, draw=False, workers=1), progress=False)
    lines = (out / "metrics.csv").read_text(encoding="utf-8").splitlines()
    ncols = len(lines[0].split(","))
    assert all(len(ln.split(",")) == ncols for ln in lines[1:])


def test_empty_source_yields_zero_summary(tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    result = pipeline.run(_spec(empty, [Stage("bg_stats")], tmp_path / "o", draw=False), progress=False)
    assert result["summary"]["n_items"] == 0
    assert result["summary"]["throughput"] == 0.0


def test_run_without_stages_raises(dataset, tmp_path):
    with pytest.raises(ValueError):
        pipeline.run(_spec(dataset, [], tmp_path / "o", draw=False), progress=False)


def test_measure_speedup_reports_consistency(dataset, tmp_path):
    report = pipeline.measure_speedup(
        _spec(dataset, [Stage("bg_stats"), Stage("image_quality")], tmp_path / "sp", draw=False),
        workers=3,
    )
    assert report["n_items"] == 4
    assert report["same_results"] is True
    assert report["serial_seconds"] > 0 and report["parallel_seconds"] > 0
    assert report["speedup"] > 0


def test_throughput_is_finite(dataset, tmp_path):
    result = pipeline.run(_spec(dataset, [Stage("bg_stats")], tmp_path / "t", draw=False,
                                workers=2), progress=False)
    assert result["summary"]["throughput"] >= 0
    assert result["summary"]["items_per_minute"] >= 0
    assert set(result["summary"]["modality_counts"]) <= {"image", "cube", "series", "event"}


def test_explicit_uris_run_only_the_picked_items(dataset, tmp_path):
    """界面上勾的那几路要能原样交给流水线，而不是重新扫一遍目录。"""
    from mdva import io

    picked = [i for i in io.scan(str(dataset)) if i.modality in ("image", "series")]
    assert picked
    spec = _spec(dataset, [Stage("bg_stats")], tmp_path / "uris", draw=False, workers=2,
                 uris=[i.uri for i in picked])
    result = pipeline.run(spec, progress=False)
    assert result["summary"]["n_items"] == len(picked)
    assert {Path(r["uri"]).name for r in result["records"]} == {Path(i.uri).name for i in picked}


def test_index_chooses_the_slice_of_a_cube(dataset, tmp_path):
    from mdva import io

    cube = next(i for i in io.scan(str(dataset)) if i.modality == "cube")
    assert cube.n_views > 3

    medians = {}
    for idx in (0, 3):
        spec = _spec(Path(cube.uri).parent, [Stage("bg_stats")], tmp_path / f"i{idx}",
                     draw=False, workers=1, uris=[cube.uri], index=idx)
        assert spec.slice.index == idx
        result = pipeline.run(spec, progress=False)
        assert result["summary"]["n_ok"] == 1
        medians[idx] = result["records"][0]["metrics"]["bg_stats.peak"]
    assert medians[0] != medians[3], "换了切片号却算出同一个峰值，说明 index 没传下去"


def test_per_item_annotated_png_is_recorded(dataset, tmp_path):
    """双击批量结果里的某一行要能打开对应标注图，靠的就是 record 里的 annotated 路径。"""
    out = tmp_path / "art2"
    result = pipeline.run(_spec(dataset, [Stage("bg_stats")], out, draw=True, workers=1),
                          progress=False)
    done = [r for r in result["records"] if r["ok"]]
    assert done
    assert all(Path(r["annotated"]).is_file() for r in done)
    names = [Path(r["annotated"]).name for r in done]
    assert len(set(names)) == len(names), "同名项的标注图互相覆盖了"
    assert (out / "annotated" / "overview.png").is_file()
