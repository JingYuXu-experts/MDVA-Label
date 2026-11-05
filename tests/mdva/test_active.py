"""主动学习闭环测试：不确定性排序、准入门槛、多轮标注与精度曲线。

这里跑的是"最可疑的先标"这条链路。oracle 用真值模拟器代替真人，
是为了让 E3（已标比例–精度曲线）能自动重放；模拟器故意掺了漏标和位置抖动，
否则曲线永远是漂亮的 100%，那种数字没有说服力。
"""

import json

import numpy as np
import pytest
from PIL import Image

from mdva.annotate.active import (SimulatedAnnotator, evaluate_labels, hard_examples,
                                  pick_round, propagate_with_gate, run_loop, stop_advice,
                                  uncertainty)
from mdva.annotate.model import Ann
from mdva.annotate.store import Store
from mdva.backends.base import Prompt


def blobs(size=160, positions=((22, 26), (84, 100), (132, 44)), amp=420.0, seed=0):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    arr = rng.normal(30.0, 2.0, (size, size))
    for cx, cy in positions:
        arr += amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 3.0 ** 2))
    arr = (arr - arr.min()) / (arr.max() - arr.min()) * 255.0
    return arr.astype(np.uint8)


@pytest.fixture
def fields(tmp_path):
    """三张单像素宇宙线图 + 与之对应的逐目标真值。"""
    truth = {"payload_truth": {}}
    paths = []
    for k in range(3):
        rng = np.random.default_rng(100 + k)
        yy, xx = np.mgrid[0:160, 0:160]
        arr = rng.normal(30.0, 2.0, (160, 160))
        spots = [(float(rng.uniform(12, 148)), float(rng.uniform(12, 148))) for _ in range(5)]
        for cx, cy in spots:
            arr[int(round(cy)), int(round(cx))] += 4000.0
        img = (arr - arr.min()) / (arr.max() - arr.min()) * 255.0
        p = tmp_path / f"xray_field_{k}.png"
        Image.fromarray(img.astype(np.uint8)).save(str(p))
        paths.append(str(p))
        truth["payload_truth"][f"xray_field_{k}"] = {"centroids": [[c[0], c[1]] for c in spots],
                                                     "cosmic": [[c[0], c[1]] for c in spots]}
    truth["paths"] = paths
    return truth, paths


@pytest.fixture
def store(tmp_path, fields):
    s = Store(tmp_path / "mdva.sqlite")
    did = s.create_dataset("主动学习", "detection", tmp_path)
    vid = s.put_version(did, None, "init")
    yield s, did, vid
    s.close()


# ---------------------------------------------------------------------- 排序

def test_uncertainty_ranks_items_and_explains_the_number(fields):
    _truth, paths = fields
    rows = uncertainty(paths, "cosmic_reject", {"nsigma": 3.0, "size": 3})
    assert len(rows) == 3
    assert [r["score"] for r in rows] == sorted([r["score"] for r in rows], reverse=True)
    assert all(r["modality"] == "image" and r["reason"] for r in rows)
    assert all(0.0 <= r["score"] <= 1.0 for r in rows)


def test_uncertainty_survives_a_bad_path(fields):
    _truth, paths = fields
    rows = uncertainty(paths + ["Z:/missing.png"], "cosmic_reject")
    assert len(rows) == 4
    assert any("读取失败" in r["reason"] for r in rows)


def test_uncertainty_skips_the_wrong_modality_without_crashing(fields):
    _truth, paths = fields
    rows = uncertainty(paths, "dynamic_threshold")
    assert rows and all(r["score"] == 0.0 and r["reason"] == "模态不匹配" for r in rows)


def test_pick_round_respects_budget_and_skips_labeled(fields):
    _truth, paths = fields
    ranked = [{"uri": p, "score": 1.0 - i * 0.1} for i, p in enumerate(paths)]
    first = pick_round(ranked, 2)
    assert [r["uri"] for r in first] == paths[:2]
    second = pick_round(ranked, 2, [p for p in paths[:2]])
    assert [r["uri"] for r in second] == [paths[2]]
    assert pick_round(ranked, 0) == []
    assert pick_round(ranked, 5, min_score=0.95) == [ranked[0]]


# ---------------------------------------------------------------------- 门槛

def test_loop_refuses_a_backend_below_the_gate(store, fields):
    s, did, vid = store
    _truth, paths = fields
    table = [{"backend": "cosmic_reject", "task": "reject_cosmic", "category": "*",
              "modality": "image", "precision": 0.5, "recall": 0.6, "f1": 0.55,
              "latency_ms": 10.0, "n_items": 3, "kind": "pr", "note": ""}]
    out = run_loop(s, vid, paths, "cosmic_reject",
                   SimulatedAnnotator(_truth, "reject_cosmic"), _truth, "reject_cosmic",
                   min_f1=0.9, table=table, rounds=2, budget=2)
    assert out["ok"] is False
    assert "0.55" in out["gate"]["reason"] and "门槛" in out["gate"]["reason"]
    assert out["history"] == []
    assert [v["id"] for v in s.list_versions(did)] == [vid]      # 被拒时连版本都不该新建


def test_gate_can_be_consciously_disabled(store, fields):
    s, did, vid = store
    _truth, paths = fields
    table = [{"backend": "cosmic_reject", "task": "reject_cosmic", "category": "*",
              "modality": "image", "precision": 0.5, "recall": 0.6, "f1": 0.55,
              "latency_ms": 10.0, "n_items": 3, "kind": "pr", "note": ""}]
    out = run_loop(s, vid, paths, "cosmic_reject",
                   SimulatedAnnotator(_truth, "reject_cosmic", drop=0.0, jitter_px=0.0),
                   _truth, "reject_cosmic", min_f1=0.9, table=table, rounds=1, budget=1,
                   enforce_gate=False)
    assert out["ok"] and out["gate"]["reason"] == "跳过门槛"


# ---------------------------------------------------------------------- 多轮闭环

def test_run_loop_grows_the_labeled_ratio_round_by_round(store, fields):
    s, did, vid = store
    truth, paths = fields
    oracle = SimulatedAnnotator(truth, "reject_cosmic", drop=0.0, jitter_px=0.0, seed=1)
    out = run_loop(s, vid, paths, "cosmic_reject", oracle, truth, "reject_cosmic",
                   params={"nsigma": 3.0, "size": 3}, min_f1=0.5, table=[
        {"backend": "cosmic_reject", "task": "reject_cosmic", "category": "*", "modality": "image",
         "precision": 1.0, "recall": 1.0, "f1": 1.0, "latency_ms": 9.0, "n_items": 3,
         "kind": "pr", "note": "实测"}], rounds=3, budget=1)
    history = out["history"]
    assert len(history) == 3
    assert [h["labeled_items"] for h in history] == [1, 2, 3]
    assert [h["labeled_ratio"] for h in history] == [round(1 / 3, 4), round(2 / 3, 4), 1.0]
    assert history[-1]["f1"] == pytest.approx(1.0)             # 完美 oracle 应当满分
    ops = [v["op"] for v in s.list_versions(did)]
    assert ops[:2] == ["init", "active_round_1"] and "active_round_3" in ops
    assert "未通过" not in out["stop_advice"]
    rows = s.list_annotations(out["version"])
    assert rows and all(r["state"] == "submitted" for r in rows)
    assert all(r["ann"].source == "manual" for r in rows)      # 标的是"人"给的，不是机器


def test_noisier_labels_give_a_lower_ceiling(store, fields):
    """同一批样本，标注质量差一档，精度曲线就要低一档——否则这条曲线是假的。"""
    s, did, vid = store
    truth, paths = fields
    table = [{"backend": "cosmic_reject", "task": "reject_cosmic", "category": "*",
              "modality": "image", "precision": 1.0, "recall": 1.0, "f1": 1.0,
              "latency_ms": 9.0, "n_items": 3, "kind": "pr", "note": ""}]
    clean = run_loop(s, vid, paths, "cosmic_reject",
                     SimulatedAnnotator(truth, "reject_cosmic", drop=0.0, jitter_px=0.0),
                     truth, "reject_cosmic", params={"nsigma": 3.0, "size": 3},
                     min_f1=0.5, table=table, rounds=3, budget=3)
    noisy = run_loop(s, vid, paths, "cosmic_reject",
                     SimulatedAnnotator(truth, "reject_cosmic", drop=0.6, jitter_px=12.0,
                                        seed=7),
                     truth, "reject_cosmic", params={"nsigma": 3.0, "size": 3},
                     min_f1=0.5, table=table, rounds=3, budget=3)
    assert clean["history"][-1]["f1"] > noisy["history"][-1]["f1"]


def test_loop_stops_when_more_labels_stop_helping(store, fields):
    s, did, vid = store
    truth, paths = fields
    advice = stop_advice([{"f1": 0.9}, {"f1": 0.85}, {"f1": 0.8}])
    assert "没有提升" in advice and "0.90" in advice
    assert stop_advice([{"f1": 0.5}, {"f1": 0.7}, {"f1": 0.9}]) == ""
    assert stop_advice([{"f1": 0.5}]) == ""


# ---------------------------------------------------------------------- 难例回流

def test_rejected_examples_jump_the_queue(store, fields):
    s, did, vid = store
    _truth, paths = fields
    aid = s.put_annotation(vid, paths[2], Ann("box", [1, 1, 9, 9], "blob"))
    s.set_state(aid, "annotator", "submit")
    s.set_state(aid, "reviewer", "reject", "框歪了")
    out = hard_examples(s, vid, paths, "cosmic_reject", k=2, params={"nsigma": 3.0, "size": 3})
    assert out["n_rejected"] == 1
    assert out["queue"][0]["uri"] == paths[2] and out["queue"][0]["why"] == "审核驳回"
    assert len(out["queue"]) == 2


# ---------------------------------------------------------------------- 带门槛的传播

def test_propagate_with_gate_blocks_and_allows(store, fields):
    s, did, vid = store
    truth, paths = fields
    cx, cy = truth["payload_truth"]["xray_field_0"]["centroids"][0]
    prompt = Prompt("box", [cx - 6, cy - 6, cx + 6, cy + 6], "b")
    bad = [{"backend": "visual_prompt", "task": "detect_similar", "category": "*",
            "modality": "image", "precision": 0.2, "recall": 0.2, "f1": 0.2,
            "latency_ms": 20.0, "n_items": 3, "kind": "pr", "note": ""}]
    out = propagate_with_gate(s, vid, paths, prompt, source=paths[0],
                              params={"threshold": 0.55}, min_f1=0.9, table=bad,
                              enforce_gate=True)
    assert out["ok"] is False and out["written"] is None
    assert len(s.list_versions(did)) == 1                      # 被拦下时不写库

    good = propagate_with_gate(s, vid, paths, prompt, source=paths[0],
                               params={"threshold": 0.55}, min_f1=0.1, table=bad,
                               enforce_gate=True)
    assert good["ok"] and good["written"]["added"] > 0
    assert good["counts"][paths[1]] >= 1
    assert len(s.list_versions(did)) == 2


# ---------------------------------------------------------------------- 标注精度

def test_evaluate_labels_on_hand_made_labels(store, fields):
    truth, paths = fields
    entry = truth["payload_truth"]["xray_field_0"]["centroids"]
    labeled = {paths[0]: [Ann("box", [x - 8, y - 8, x + 8, y + 8], "blob") for x, y in entry],
               paths[1]: [Ann("point", [0, 0], "blob")]}
    out = evaluate_labels(labeled, truth, "reject_cosmic")
    assert out["n_truth"] == 10 and out["n_labeled"] == 6
    assert out["precision"] == pytest.approx(5 / 6, abs=1e-3)
    assert out["recall"] == pytest.approx(0.5)
    assert "命中" in out["note"]


def test_evaluate_labels_reports_when_the_task_has_no_scorer(fields):
    _truth, paths = fields
    out = evaluate_labels({paths[0]: [Ann("interval", [0, 1], "x")]}, {}, "content_filter")
    assert out["kind"] == "skipped" and "评测量规" in out["note"]


# ---------------------------------------------------------------------- 时序闭环

def test_series_loop_uses_absolute_times(tmp_path):
    from mdva import datasets

    root = tmp_path / "data"
    built = datasets.build_aerospace(root, seed=11, n_telemetry=1200, n_frames=2)
    truth = json.loads((root / "aerospace" / "truth.json").read_text(encoding="utf-8"))
    csv_uri = str(root / "aerospace" / "telemetry" / "engineering.csv")

    store = Store(tmp_path / "mdva.sqlite")
    did = store.create_dataset("遥测主动学习", "series_interval", root)
    vid = store.put_version(did, None, "init")
    # channel=1 是 Bus_Voltage：注入过母线毛刺的那一路，SA_Current 本身是干净的
    ranked = uncertainty([csv_uri], "dynamic_threshold", {"nsigma": 5.0, "channel": 1})
    assert ranked[0]["score"] > 0 and ranked[0]["modality"] == "series"

    out = run_loop(store, vid, [csv_uri], "dynamic_threshold",
                   SimulatedAnnotator(truth, "series_anomaly", drop=0.0, seed=3),
                   truth, "series_anomaly", domain="aerospace", rounds=2, budget=1,
                   min_f1=0.0, table=[], enforce_gate=False,
                   params={"nsigma": 5.0, "channel": 1})
    assert out["ok"] and out["history"]
    last = out["history"][-1]
    assert last["labeled_items"] == 1
    assert 0.0 < last["f1"] <= 1.0                      # 真值区间命中，但漏标会压住上限
    rows = store.list_annotations(out["version"])
    assert rows and all(r["ann"].geom == "interval" for r in rows)
    t_ref = 1735689600.0
    assert all(r["ann"].coords[0] > t_ref for r in rows)      # 落库的是绝对时刻不是序号
    assert built["truth"]["injected_anomalies"]
    store.close()
