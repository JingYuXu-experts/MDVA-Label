"""后端层测试：注册表、视觉提示、评测与推荐。

评测与推荐是这层的重点：推荐语里的每个数字都必须来自实测表，
而未实测 / 实测为 0 的后端必须被压下去，不能让"快但没用"的算法排到第一。
"""

import json

import numpy as np
import pytest

from mdva import analysis
from mdva.annotate.model import Ann
from mdva.backends import base as BASE
from mdva.backends import bench as BN
from mdva.backends import classic as CL
from mdva.backends import dl_stub as DL
from mdva.backends import probe as PR
from mdva.backends import recommend as RC
from mdva.backends import visual as VS
from mdva.core.model import DataItem, Modality, Slice


def blob_image(size=160, positions=((22, 26), (84, 100), (132, 44)), amp=420.0, sigma=6.0,
               seed=0):
    """三团一样的亮斑 + 噪声背景：视觉提示的"同图找相似"有确定的地面真值。"""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    arr = rng.normal(30.0, 2.0, (size, size))
    for cx, cy in positions:
        arr += amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma ** 2))
    return np.clip(arr, 0, None).astype(np.float32)


def array_item(arr, modality="image", n_views=1, uri="synthetic://x", t_ref=None):
    return DataItem(id=uri, modality=modality, uri=uri, title=uri, shape=arr.shape,
                    dtype=str(arr.dtype), n_views=n_views, t_ref=t_ref,
                    t_end=None if t_ref is None else t_ref + arr.shape[-1],
                    _opener=lambda a=arr: a)


@pytest.fixture
def image():
    return array_item(blob_image())


@pytest.fixture
def cube():
    arr = np.stack([blob_image(seed=s) for s in range(4)])
    return array_item(arr, modality="cube", n_views=4)


# ---------------------------------------------------------------------- 注册表

def test_registry_has_nine_classic_one_visual_three_stubs():
    names = BASE.BACKENDS.names()
    assert len(CLASSIC_NAMES()) == len(analysis.names()) == 9
    assert "visual_prompt" in names
    assert [n for n in names if n.startswith("deep:")] == ["deep:grounding_dino", "deep:trex2",
                                                           "deep:yolo_world"]


def CLASSIC_NAMES():
    return [n for n in BASE.BACKENDS.names()
            if BASE.BACKENDS.get(n).create().manifest.family == "classic"]


def test_stubs_are_reported_unavailable_with_a_reason():
    for name in [n for n in BASE.BACKENDS.names() if n.startswith("deep:")]:
        backend = BASE.pick(name)
        ok, why = backend.available(PR.detect())
        assert not ok and "不接入深度学习" in why
        assert backend.manifest.needs_gpu and backend.manifest.needs_weights
        assert backend.vram_gb > 0
        with pytest.raises(RuntimeError):
            backend.predict(array_item(blob_image()))


def test_catalog_marks_availability_and_keeps_params():
    cat = BASE.catalog()
    assert len(cat) == len(BASE.BACKENDS.names())
    visual = next(c for c in cat if c["name"] == "visual_prompt")
    assert visual["available"] and visual["supports_prompt"]
    assert set(visual["params"]) >= {"threshold", "max_matches", "min_sep"}
    assert all(not c["available"] for c in cat if c["name"].startswith("deep:"))


def test_each_classic_backend_reuses_the_analyzer_schema():
    from mdva.core.registry import ANALYZERS

    for name in analysis.names():
        backend = BASE.BACKENDS.get(name).create()
        entry = ANALYZERS.get(name)
        assert sorted(backend.manifest.schema) == sorted(entry.schema)
        assert backend.manifest.modalities == tuple(entry.factory.modality)
        assert backend.manifest.family == "classic"


def test_pick_reports_allowed_params():
    backend = BASE.pick("star_detect", nsigma=5.0)
    assert backend.params["nsigma"] == 5.0
    with pytest.raises(TypeError) as exc:
        BASE.pick("star_detect", nsigmaa=5.0)
    assert "nsigma" in str(exc.value)


# ---------------------------------------------------------------------- 经典后端预测

def test_predict_returns_anns_tagged_with_the_backend(image):
    backend = BASE.pick("star_detect", nsigma=4.0, max_sources=10)
    anns = backend.predict(image)
    assert anns and all(isinstance(a, Ann) for a in anns)
    assert {a.backend for a in anns} == {"star_detect"}
    assert {a.source for a in anns} == {"preannotate"}
    assert all(a.geom in ("box", "point") for a in anns)


def test_classic_backend_refuses_the_wrong_modality(image):
    with pytest.raises(ValueError):
        BASE.pick("dynamic_threshold").predict(image)


def test_cube_annotations_keep_their_slice(cube):
    anns = BASE.pick("star_detect", max_sources=20).predict(cube, window=Slice.at_index(2))
    assert anns and {a.slice for a in anns} == {2}


def test_metric_only_backends_produce_no_annotations(image):
    """bg_stats / image_quality 只给指标不画框，界面必须能提前知道这一点。"""
    for name in ("bg_stats", "image_quality"):
        assert BASE.pick(name).predict(image) == []
        assert BASE.pick(name).metrics(image)


def test_overlay_roundtrip_is_lossless(image):
    """后端预览（draw_overlay）与网页画布必须画同一批几何，坐标不能过一次转换就变。"""
    for ann in BASE.pick("star_detect").predict(image):
        back = Ann.from_overlay(ann.to_overlay(), source=ann.source, backend=ann.backend,
                               slice=ann.slice)
        assert back.geom == ann.geom
        assert np.allclose(back.coords, ann.coords, atol=1e-6)
        assert back.label == ann.label


def test_series_backend_emits_intervals_with_absolute_times():
    t = 1735689600.0 + np.arange(400, dtype=np.float64)
    values = np.zeros((400, 1))
    values[200:240, 0] = 9.0
    from mdva.core.model import TimeSeries
    ts = TimeSeries(t=t, values=values, channels=["v"], units=[""], discrete=[False])
    item = DataItem(id="s", modality="series", uri="synthetic://s", title="s",
                    shape=values.shape, t_ref=float(t[0]), t_end=float(t[-1]),
                    _opener=lambda: ts)
    anns = BASE.pick("dynamic_threshold", nsigma=3.0, channel=0).predict(
        item, Slice.at_time(ts.start, ts.end))
    assert anns and all(a.geom in ("interval", "level") for a in anns)
    assert min(a.coords[0] for a in anns) >= t[0] and max(a.coords[1] for a in anns) <= t[-1]


# ---------------------------------------------------------------------- 视觉提示

def test_box_prompt_finds_every_similar_blob_in_the_same_image(image):
    backend = VS.VisualPromptBackend(threshold=0.55, min_sep=8.0, max_matches=10)
    prompt = VS.Prompt("box", [12, 16, 32, 36], "耀斑区")
    anns = backend.find_similar(image, prompt)
    centers = [(np.mean(a.coords[0::2]), np.mean(a.coords[1::2])) for a in anns]
    for cx, cy in ((22, 26), (84, 100), (132, 44)):
        assert min(np.hypot(cx - px, cy - py) for px, py in centers) < 8.0
    assert all(a.source == "visual_prompt" and a.backend == "visual_prompt" for a in anns)
    assert all(a.confidence > 0.5 for a in anns)
    scores = [a.confidence for a in anns]
    assert scores == sorted(scores, reverse=True)


def test_raising_the_threshold_reduces_the_hits(image):
    """参数改了必须看得见区别——这条同时是用户验收门槛和回归防护。"""
    loose = VS.VisualPromptBackend(threshold=0.40, max_matches=40)
    strict = VS.VisualPromptBackend(threshold=0.90, max_matches=40)
    prompt = VS.Prompt("box", [12, 16, 32, 36], "blob")
    assert len(strict.predict(image, [prompt])) <= len(loose.predict(image, [prompt]))
    assert len(loose.predict(image, [prompt])) >= 3


def test_point_and_poly_prompts_also_work(image):
    by_point = VS.VisualPromptBackend(threshold=0.55).predict(image, [VS.Prompt("point", [84, 100], "b")])
    by_poly = VS.VisualPromptBackend(threshold=0.55).predict(
        image, [VS.Prompt("poly", [74, 90, 94, 90, 94, 110, 74, 110], "b")])
    assert by_point and by_poly
    assert all(a.geom == "box" for a in by_point + by_poly)


def test_prompt_template_propagates_across_items():
    """跨图传播：第二个数据项没见过第一个图，模板随提示一起带过去。"""
    src = array_item(blob_image(seed=1))
    dst = array_item(blob_image(seed=2, positions=((40, 120), (110, 60), (20, 20))))
    backend = VS.VisualPromptBackend(threshold=0.55, min_sep=8.0)
    prompt = VS.Prompt("box", [12, 16, 32, 36], "blob")
    prompt.template = np.asarray(src.load())[16:36, 12:32]
    anns = backend.predict(dst, [prompt])
    assert anns, "带模板的传播应该能找到目标"
    assert all(a.coords[2] - a.coords[0] == 20 for a in anns)   # 框大小跟随模板


def test_flat_region_never_matches(image):
    """空白处的"命中"必须是 0：噪声上的高 NCC 会把标注污染成假阳性。"""
    flat = array_item(np.full((160, 160), 30.0))
    backend = VS.VisualPromptBackend(threshold=0.55, min_sep=8.0)
    assert backend.predict(flat, [VS.Prompt("box", [12, 16, 32, 36], "b")]) == []


def test_prompt_validation():
    with pytest.raises(ValueError):
        VS.Prompt("text", [0, 0, 1, 1], "猫")
    with pytest.raises(ValueError):
        VS.Prompt("box", [0, 0])
    with pytest.raises(ValueError):
        VS.Prompt("poly", [0, 0, 1, 1])
    with pytest.raises(ValueError):
        VS.Prompt("box", [0, 0, 1, 1], role="maybe")
    p = VS.Prompt("box", [1, 2, 3, 4], "x", source_uri="a.fits")
    assert VS.Prompt.from_json(p.to_json()) == p
    assert p.to_ann().geom == "box"
    with pytest.raises(ValueError):
        VS.VisualPromptBackend.as_prompt(Ann("interval", [0, 1]))


# ---------------------------------------------------------------------- 机器档位

def test_probe_reports_hardware_without_ever_claiming_gpu():
    p = PR.detect()
    assert p.cpu_count >= 1 and p.ram_gb > 0
    assert p.has_gpu is False and "深度学习" in p.note
    assert p.tier() in ("low", "mid", "high")
    assert p.max_workers(10) <= min(8, p.cpu_count)
    assert p.can_load_points(20000, 6)
    assert not p.can_load_points(int(1e9), 64)
    assert set(p.to_json()) >= {"cpu_count", "ram_gb", "has_gpu", "tier", "note"}


def test_probe_tier_boundaries():
    assert PR.Probe(cpu_count=16, ram_gb=64).tier() == "high"
    assert PR.Probe(cpu_count=4, ram_gb=16).tier() == "mid"
    assert PR.Probe(cpu_count=2, ram_gb=4).tier() == "low"


# ---------------------------------------------------------------------- 评测

def test_point_matcher_counts_greedy_one_to_one():
    preds = [(0.0, 0.0), (10.0, 10.0)]
    truths = [(1.0, 1.0), (11.0, 11.0), (200.0, 200.0)]
    assert BN._match_points(preds, truths, tol=3.0) == (2, 0, 1)
    assert BN._match_points([], truths, tol=3.0) == (0, 0, 3)
    assert BN._match_points(preds, [], tol=3.0) == (0, 2, 0)


def test_interval_matcher_accepts_a_point_inside_the_truth():
    truth = [(100.0, 200.0)]
    assert BN._match_intervals([(150.0, 150.0)], truth) == (1, 0, 0)
    assert BN._match_intervals([(300.0, 320.0)], truth) == (0, 1, 1)
    assert BN._match_intervals([(90.0, 210.0)], truth) == (1, 0, 0)
    # 两条预测都覆盖同一条真值时，只算一次命中，多出来的是误检
    assert BN._match_intervals([(90.0, 210.0), (110.0, 190.0)], truth) == (1, 1, 0)


def test_degraded_copy_is_different_but_same_shape(image):
    bad = BN.degraded(image)
    a, b = np.asarray(image.load()), np.asarray(bad.load())
    assert a.shape == b.shape and bad.title != image.title
    assert not np.allclose(a, b)
    assert np.nanmax(b) <= np.nanmax(a) + 1e-6       # 削顶只会变小


def test_cosmic_view_does_not_mutate_the_shared_truth():
    truth = {"payload_truth": {"a": {"centroids": [[1, 1]], "cosmic": [[5, 5]]}}}
    swapped = BN._cosmic_view(truth)
    assert swapped["payload_truth"]["a"]["centroids"] == [[5, 5]]
    assert truth["payload_truth"]["a"]["centroids"] == [[1, 1]]      # 原件不动


def test_correlation_self_check_finds_the_coupled_pair():
    item = BN.coupled_item(seed=7)
    out = BN.measure(BASE.pick("correlation"), [item], {}, "correlation")
    assert out["f1"] == 1.0 and "A~B" in out["note"]


def test_measure_dispatches_by_task_and_says_when_truth_is_missing():
    image_item = array_item(blob_image())
    out = BN.measure(BASE.pick("star_detect"), [image_item], {}, "detect_source")
    assert out["kind"] == "skipped" and "真值" in out["note"]
    unknown = BN.measure(BASE.pick("star_detect"), [image_item], {}, "no_such_task")
    assert unknown["kind"] == "skipped" and "评测量规" in unknown["note"]


def test_quality_scoring_is_pairwise_not_fake_precision():
    items = [array_item(blob_image(seed=s)) for s in range(3)]
    out = BN.measure(BASE.pick("bg_stats"), items, {}, "quality_flag")
    assert out["kind"] == "pairwise" and 0.0 <= out["f1"] <= 1.0
    assert "成对判别" in out["note"]


def test_bench_run_on_real_generated_data(tmp_path):
    from mdva import datasets

    root = tmp_path / "data"
    datasets.build_aerospace(root, seed=5, n_telemetry=1500, n_frames=2)
    rows = BN.run(root, limit=2, only=["cosmic_reject", "dynamic_threshold"])
    by = {r.backend: r for r in rows}
    assert set(by) == {"cosmic_reject", "dynamic_threshold"}
    assert by["cosmic_reject"].f1 > 0.5 and by["cosmic_reject"].latency_ms > 0
    assert 0.0 <= by["dynamic_threshold"].f1 <= 1.0
    path = BN.save_table(rows, root / "bench.json")
    back = BN.load_table(path)
    assert back[0]["backend"] in by and back[0]["task"]
    assert BN.load_table(root / "missing.json") == []      # 没测过就是空表，不编数据


# ---------------------------------------------------------------------- 命令行

def test_cli_bench_tables_recommendations_and_gate(tmp_path, capsys):
    from mdva import cli, datasets

    root = tmp_path / "data"
    datasets.build_aerospace(root, seed=5, n_telemetry=1500, n_frames=2)
    table = root / "bench.json"
    code = cli.main(["bench", str(root), "--limit", "3", "--only", "cosmic_reject",
                     "--out", str(table), "--recommend", "2",
                     "--domain", "aerospace", "--task", "reject_cosmic"])
    assert code == 0
    out = capsys.readouterr().out
    assert "实测表写入" in out and "cosmic_reject" in out and "综合分" in out
    assert table.is_file()

    # 门槛 1.01 一定拦住：证明"禁用"是被实测数字挡住的，不是界面写死的
    assert cli.main(["bench", str(root), "--reuse", "--out", str(table),
                     "--check", "cosmic_reject", "--task", "reject_cosmic",
                     "--domain", "aerospace", "--threshold", "1.01"]) == 1
    assert "禁用" in capsys.readouterr().out
    assert cli.main(["bench", str(tmp_path / "nowhere")]) == 2
    assert "数据目录不存在" in capsys.readouterr().out


# ---------------------------------------------------------------------- 推荐

@pytest.fixture
def table():
    return [
        {"backend": "cosmic_reject", "task": "reject_cosmic", "category": "aerospace",
         "modality": "image", "precision": 1.0, "recall": 0.9, "f1": 0.947,
         "latency_ms": 12.0, "n_items": 3, "kind": "pr", "note": "容差 6.0 px"},
        {"backend": "dynamic_threshold", "task": "series_anomaly", "category": "aerospace",
         "modality": "series", "precision": 0.4, "recall": 0.8, "f1": 0.53,
         "latency_ms": 9.0, "n_items": 2, "kind": "pr", "note": "真值区间 8 段"},
        {"backend": "rolling_stats", "task": "series_anomaly", "category": "aerospace",
         "modality": "series", "precision": 0.0, "recall": 0.0, "f1": 0.0,
         "latency_ms": 1.0, "n_items": 2, "kind": "pr", "note": "真值区间 8 段"},
        {"backend": "iforest", "task": "series_anomaly", "category": "aerospace",
         "modality": "series", "precision": 0.3, "recall": 0.6, "f1": 0.4,
         "latency_ms": 900.0, "n_items": 2, "kind": "pr", "note": ""},
    ]


def test_ranking_prefers_measured_accuracy_over_raw_speed(table):
    ranked = RC.rank("aerospace", "series_anomaly", table=table, probe=PR.Probe(8, 32))
    names = [r["backend"] for r in ranked]
    assert names.index("dynamic_threshold") < names.index("iforest")
    assert names.index("iforest") < names.index("rolling_stats")   # F1=0 被压到最后
    slow = next(r for r in ranked if r["backend"] == "rolling_stats")
    assert any("实测 F1 为 0" in r for r in slow["reasons"])


def test_reasons_quote_measured_numbers(table):
    item = RC.score("dynamic_threshold", "series_anomaly", "aerospace", table=table,
                    probe=PR.Probe(8, 32))
    text = " ".join(item["reasons"])
    assert "0.53" in text and "9.0 ms" in text and "P 0.40" in text
    assert item["measured"] and item["f1"] == pytest.approx(0.53)


def test_unmeasured_backend_is_discounted_and_says_so():
    item = RC.score("changepoint", "series_anomaly", "aerospace", table=[],
                    probe=PR.Probe(4, 16))
    assert not item["measured"]
    assert any("还没实测" in r for r in item["reasons"])
    ok, f1, why = RC.usable("changepoint", "series_anomaly", "aerospace", 0.9, table=[])
    assert not ok and "先运行 mdva bench" in why


def test_ninety_percent_gate_uses_the_measured_value(table):
    ok, f1, why = RC.usable("cosmic_reject", "reject_cosmic", "aerospace", 0.9, table=table)
    assert ok and f1 == pytest.approx(0.947) and "≥ 门槛" in why
    ok, f1, why = RC.usable("dynamic_threshold", "series_anomaly", "aerospace", 0.9, table=table)
    assert not ok and f1 == pytest.approx(0.53)
    assert "实测 F1 0.53 < 门槛 0.90" in why and "已禁用" in why
    ok, _f1, why = RC.usable("deep:trex2", "content_filter", "aerospace", 0.9, table=table)
    assert not ok and "不接入深度学习" in why


def test_top3_respects_domain_and_task(table):
    top = RC.top3("aerospace", "reject_cosmic", table=table, probe=PR.Probe(8, 32))
    assert top[0]["backend"] == "cosmic_reject"
    assert len(top) >= 1
    assert all("score" in r and "reasons" in r for r in top)
    full = RC.rank("aerospace", "series_anomaly", table=table, include_unavailable=True,
                   probe=PR.Probe(8, 32))
    assert any(r["backend"].startswith("deep:") and not r["available"] for r in full)


def test_score_falls_back_to_cross_domain_rows(table):
    item = RC.score("cosmic_reject", "reject_cosmic", "medical", table=table,
                    probe=PR.Probe(8, 32))
    assert item["measured"] and item["f1"] == pytest.approx(0.947)


@pytest.mark.parametrize("module", ["mdva.backends", "mdva.backends.bench",
                                   "mdva.backends.visual", "mdva.annotate",
                                   "mdva.annotate.prompts", "mdva.annotate.active",
                                   "mdva.clean", "mdva.augment", "mdva.export",
                                   "mdva.web.services", "mdva.cli"])
def test_every_entry_module_imports_standalone(module):
    """每个入口模块都必须能被"第一个 import"拉起。

    annotate(L4) 与 backends(L5) 互相引用时，测试里因为 import 顺序恰好看不出来，
    而 CLI 的真实启动顺序是先 import mdva.cli → 再按需 import backends，
    循环一旦成环就是用户机器上的 ImportError。
    """
    import subprocess
    import sys
    from pathlib import Path

    src = str(Path(__file__).resolve().parents[2] / "src")
    code = f"import sys; sys.path.insert(0, {src!r}); import {module}"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=180)
    assert done.returncode == 0, done.stderr[-600:]
