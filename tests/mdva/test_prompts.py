"""智能标注：同图找相似、跨图传播、并行预标注与落库纪律。

这一层的重点不是"能不能画框"，而是两条：
机器产出的标注必须落到新版本（不许覆盖人工成果），以及并行的结果必须与串行一致。
"""

import numpy as np
import pytest
from PIL import Image

from mdva.annotate.model import Ann
from mdva.annotate.prompts import (Job, apply_results, measure_speedup, preannotate,
                                   propagate, resolve_prompt, same_image, score_items,
                                   undo_last)
from mdva.annotate.store import Store
from mdva.backends.base import Prompt


def blobs(size=160, positions=((22, 26), (84, 100), (132, 44)), amp=420.0, seed=0):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    arr = rng.normal(30.0, 2.0, (size, size))
    for cx, cy in positions:
        arr += amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 6.0 ** 2))
    arr = (arr - arr.min()) / (arr.max() - arr.min()) * 255.0
    return arr.astype(np.uint8)         # PNG 存不了 float，落盘前先归一到 8 位


@pytest.fixture
def pngs(tmp_path):
    """三张各有三个亮斑的 PNG，位置互不相同——传播结果有确定的期望。"""
    paths = []
    for k, pos in enumerate((((22, 26), (84, 100), (132, 44)),
                             ((40, 120), (110, 60), (20, 30)),
                             ((70, 70), (140, 20), (30, 140)))):
        p = tmp_path / f"field_{k}.png"
        Image.fromarray(blobs(positions=pos, seed=k)).save(str(p))
        paths.append(str(p))
    return paths


@pytest.fixture
def store(tmp_path, pngs):
    s = Store(tmp_path / "mdva.sqlite")
    did = s.create_dataset("亮斑传播", "detection", tmp_path)
    vid = s.put_version(did, None, "init")
    s.set_labels(did, [{"name": "blob"}, {"name": "耀斑区"}])
    yield s, did, vid
    s.close()


# ---------------------------------------------------------------------- 提示与匹配

def test_same_image_fills_in_the_other_instances(pngs):
    from mdva import io

    item = io.load_any(pngs[0])
    anns = same_image(item, Prompt("box", [12, 16, 32, 36], "耀斑区"),
                      params={"threshold": 0.55, "min_sep": 8.0})
    assert len(anns) >= 3
    assert {a.source for a in anns} == {"visual_prompt"}
    assert all(a.label == "耀斑区" for a in anns)


def test_resolve_prompt_carries_the_template(pngs):
    from mdva import io

    filled = resolve_prompt(io.load_any(pngs[0]), Prompt("box", [12, 16, 32, 36], "b"))
    assert filled.template is not None and filled.template.shape == (20, 20)
    assert filled.source_uri.endswith("field_0.png")
    # 已经带模板的提示不会被再取一次
    assert resolve_prompt(None, filled).template is filled.template


def test_propagate_hits_targets_that_never_saw_the_source(pngs, store):
    s, did, vid = store
    out = propagate(pngs[1:], Prompt("box", [12, 16, 32, 36], "blob"), source=pngs[0],
                    params={"threshold": 0.55, "min_sep": 8.0}, workers=2)
    assert out["n_items"] == 2 and out["n_ok"] == 2
    assert all(len(a) >= 2 for a in out["results"].values())
    for uri, anns in out["results"].items():
        if uri.endswith("field_1.png"):
            centers = [((a.coords[0] + a.coords[2]) / 2, (a.coords[1] + a.coords[3]) / 2)
                       for a in anns]
            assert min(np.hypot(110 - cx, 60 - cy) for cx, cy in centers) < 10
    assert out["workers"] == 2 and out["speedup"] >= 0.8


def test_propagate_min_confidence_drops_weak_hits(pngs):
    loose = propagate(pngs[1:], Prompt("box", [12, 16, 32, 36], "b"), source=pngs[0],
                      params={"threshold": 0.5, "max_matches": 60})
    strict = propagate(pngs[1:], Prompt("box", [12, 16, 32, 36], "b"), source=pngs[0],
                       params={"threshold": 0.5, "max_matches": 60}, min_confidence=0.95)
    assert strict["n_annotations"] <= loose["n_annotations"]
    for anns in strict["results"].values():
        assert all(a.confidence >= 0.95 for a in anns)


def test_preannotate_classic_backend_over_files(pngs):
    out = preannotate(pngs, "star_detect", {"nsigma": 4.0, "max_sources": 12}, workers=2)
    assert out["n_ok"] == 3 and out["n_annotations"] >= 9
    assert all(a.backend == "star_detect" and a.source == "preannotate"
               for anns in out["results"].values() for a in anns)


def test_a_bad_path_does_not_kill_the_batch(pngs):
    out = preannotate([pngs[0], "Z:/nope/nothing.png"], "star_detect")
    assert out["n_ok"] == 1
    assert "nothing.png" in "".join(out["errors"]) or out["errors"]
    assert out["records"][0].count >= 3


def test_score_items_is_sorted_by_confidence(pngs):
    rows = score_items(pngs, "star_detect", params={"nsigma": 4.0})
    assert len(rows) == 3
    assert [r["score"] for r in rows] == sorted([r["score"] for r in rows], reverse=True)


# ---------------------------------------------------------------------- 并行一致性

def test_parallel_result_matches_serial(pngs):
    serial = preannotate(pngs, "star_detect", {"nsigma": 4.0}, workers=1)
    parallel = preannotate(pngs, "star_detect", {"nsigma": 4.0}, workers=4)
    assert sorted(serial["results"]) == sorted(parallel["results"])
    for uri in serial["results"]:
        assert [a.coords for a in serial["results"][uri]] == \
               [a.coords for a in parallel["results"][uri]]


def test_measure_speedup_reports_consistency(pngs):
    out = measure_speedup(pngs * 2, "star_detect", {"nsigma": 4.0}, workers=3)
    assert out["n_items"] == 6 and out["workers"] == 3
    assert out["consistent"] is True
    assert out["speedup"] > 0 and out["efficiency"] > 0
    assert isinstance(out["serial_ms"], float) and isinstance(out["parallel_ms"], float)


@pytest.fixture(scope="module")
def heavy_batch(tmp_path_factory):
    """12 路 512×512 FITS：单路 ~20 ms，够把线程池的收益测出来。"""
    pytest.importorskip("astropy")
    from mdva import synth

    root = tmp_path_factory.mktemp("heavy")
    uris = []
    for k in range(4):
        img, _truth = synth.star_field(shape=(512, 512), n_sources=60, n_cosmic=40, seed=k)
        uris.append(str(synth.write_fits(root / f"field_{k}.fits", img, {"EXPTIME": 32.0})))
    return uris * 3


def test_thread_pool_actually_speeds_annotation_up(heavy_batch):
    """实测而非声称：本机线程池在标注路径上约 1.7×（进程池在 Windows 上被
    冷启动吃光，所以默认 executor=thread 是有数据支撑的选择，不是偷懒）。"""
    out = measure_speedup(heavy_batch, "star_detect", {"nsigma": 4.0, "max_sources": 200},
                          workers=4, executor="thread")
    assert out["consistent"] is True
    assert out["n_annotations"] > 100
    assert out["speedup"] >= 1.2, out
    assert out["parallel_ms"] < out["serial_ms"]


def test_job_slice_defaults_to_the_whole_window():
    assert Job("u", "star_detect").slice().index == 0
    assert Job("u", "star_detect", window=(5.0, 9.0), index=2).slice().t0 == 5.0


# ---------------------------------------------------------------------- 落库纪律

def test_apply_results_writes_a_new_version_and_keeps_the_old_one(store, pngs):
    s, did, vid = store
    s.put_annotation(vid, pngs[0], Ann("box", [12, 16, 32, 36], "blob"))   # 人工先标了一条
    before = len(s.list_annotations(vid))

    result = propagate(pngs, Prompt("box", [12, 16, 32, 36], "blob"), source=pngs[0],
                       params={"threshold": 0.55})
    report = apply_results(s, vid, result)
    assert report["version"] != vid and report["version_is_new"]
    assert report["added"] > 0
    assert len(s.list_annotations(vid)) == before          # 原版本一条没动
    versions = s.list_versions(did)
    assert [v["op"] for v in versions] == ["init", "preannotate"]
    assert versions[-1]["parent_id"] == vid
    assert s.get_version(report["version"])["op"] == "preannotate"
    task = s.list_tasks(did, "preannotate")[0]
    assert task["state"] == "done" and task["version_out"] == report["version"]


def test_rerunning_preannotate_does_not_duplicate_boxes(store, pngs):
    s, did, vid = store
    prompt = Prompt("box", [12, 16, 32, 36], "blob")
    first = apply_results(s, vid, propagate(pngs, prompt, source=pngs[0],
                                            params={"threshold": 0.55}))
    n_first = len(s.list_annotations(first["version"]))
    second = apply_results(s, first["version"], propagate(pngs, prompt, source=pngs[0],
                                                          params={"threshold": 0.55}))
    assert second["added"] == 0                    # 一条新的都没添——父版本已经有了
    assert second["deduped"] >= n_first            # 重合的部分被 merge 掉并计数
    # 新版本是完整快照：机器结果不翻倍，人工成果也不会掉出去
    assert len(s.list_annotations(second["version"])) == n_first
    assert second["carried"] == n_first                   # 父版本那 9 条被完整带过来


def test_preannotate_keeps_human_boxes_in_the_new_version(store, pngs):
    """机器结果开新版本时，人工已经标的框必须一起带过去，否则工作集当场缩水。"""
    s, did, vid = store
    human = [Ann("box", [120, 10, 150, 40], "人工判断"), Ann("point", [5, 5], "定位点")]
    for ann in human:
        s.put_annotation(vid, pngs[1], ann, role="expert", state="approved")

    report = apply_results(s, vid, preannotate(pngs, "star_detect", {"max_sources": 5}))
    rows = s.list_annotations(report["version"])
    carried = [r for r in rows if r["ann"].source == "manual"]
    assert {a.label for a in human} <= {r["ann"].label for r in carried}
    assert all(r["state"] == "approved" and r["role"] == "expert" for r in carried)
    assert report["carried"] == len(carried) == 2
    assert report["added"] > 0                      # 机器那条也进来了
    assert len(s.list_annotations(vid)) == 2        # 父版本原样不动


def test_apply_into_current_version_when_asked(store, pngs):
    s, did, vid = store
    report = apply_results(s, vid, preannotate(pngs, "star_detect", {"max_sources": 5}),
                           new_version=False)
    assert report["version"] == vid and not report["version_is_new"]
    assert len(s.list_annotations(vid)) == report["added"] > 0
    assert s.list_tasks(did, "preannotate") == []          # 没开新版本就不留任务行


def test_undo_last_only_points_back(store, pngs):
    s, did, vid = store
    report = apply_results(s, vid, preannotate(pngs, "star_detect", {"max_sources": 5}))
    info = undo_last(s, report["version"])
    assert info["from"] == report["version"] and info["to"] == vid
    assert info["kept"] == report["version"]
    assert len(s.list_annotations(report["version"])) == info["annotations"]
    assert s.get_version(vid) is not None                  # 父版本还在，随时可切回去


def test_state_filter_still_applies_to_machine_anns(store, pngs):
    s, did, vid = store
    report = apply_results(s, vid, preannotate(pngs, "star_detect", {"max_sources": 5}),
                           state="submitted", role="annotator")
    rows = s.list_annotations(report["version"])
    assert rows and all(r["state"] == "submitted" for r in rows)
    assert all(r["ann"].source == "preannotate" for r in rows)
    assert s.queue(report["version"], "reviewer")           # 直接进审核队列
