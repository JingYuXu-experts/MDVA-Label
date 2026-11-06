"""Web 服务门面：一次调用一个界面动作，返回 JSON 安全的 dict。

这一层不依赖 fastapi，所以能在只装了科学栈的机器上完整测一遍——
路由适配器只是把这些函数接到 HTTP 上，逻辑对不对全在这里验。
"""

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from mdva import io, synth
from mdva.web.services import Hub, png_b64, slice_of, style_of


@pytest.fixture(scope="module")
def ws(tmp_path_factory):
    """一个真实的多模态合成数据集 + 一个 Hub（模块级复用，建库只做一次）。"""
    pytest.importorskip("astropy")
    root = tmp_path_factory.mktemp("ws")
    data = synth.make_dataset(root / "aerospace", n_telemetry=1200, seed=5)
    hub = Hub(root / "mdva.sqlite", out_dir=root / "out")
    yield hub, data, root
    hub.close()


@pytest.fixture
def hub_ws(ws):
    hub, data, root = ws
    created = hub.create_dataset("航天服务", "detection", data, category="aerospace",
                                labels=["耀斑区", "宇宙线"])
    return hub, data, root, created["dataset_id"], created["version_id"]


# ---------------------------------------------------------------------- 参数解析

def test_slice_of_covers_both_semantics():
    assert slice_of({"index": 3}).index == 3
    assert slice_of({"index": 3}).t0 == -np.inf
    window = slice_of({"t0": "12.5", "t1": 30})
    assert window.t0 == 12.5 and window.t1 == 30.0


def test_style_of_only_takes_keys_l2_actually_accepts():
    style = style_of({"width": 800, "height": 600, "interval": "percent", "gamma": "1.8",
                      "invert": "true", "channel": "2", "colormap": "inferno",
                      "show_title": "false"})
    assert style == {"interval": "percent", "colormap": "inferno", "gamma": 1.8,
                     "invert": True, "channel": 2}


def test_png_b64_round_trips():
    import base64
    import io as _io

    rgb = np.zeros((8, 8, 3), dtype=np.uint8)
    rgb[2:5, 2:5] = (10, 200, 30)
    img = Image.open(_io.BytesIO(base64.b64decode(png_b64(rgb))))
    assert img.size == (8, 8)
    assert np.asarray(img)[3, 3].tolist() == [10, 200, 30]


# ---------------------------------------------------------------------- 数据集与版本

def test_create_and_list_dataset(hub_ws):
    hub, data, root, did, vid = hub_ws
    row = [d for d in hub.datasets() if d["id"] == did][0]
    assert row["n_versions"] == 1 and row["head"] == vid
    assert row["meta"]["category"] == "aerospace"
    assert [l["name"] for l in hub.dataset(did)["labels"]] == ["耀斑区", "宇宙线"]


def test_create_dataset_validates_input(hub_ws):
    hub, data, root, did, vid = hub_ws
    with pytest.raises(ValueError):
        hub.create_dataset("坏类型", "segmentationx", data)
    with pytest.raises(ValueError):
        hub.create_dataset("坏目录", "detection", root / "根本没有这个目录")


def test_manifest_is_derived_from_the_dag(hub_ws):
    hub, data, root, did, vid = hub_ws
    manifest = hub.manifest(did)
    assert manifest["dataset_id"] == did
    assert (root / "aerospace" / "manifest.json").is_file()
    assert manifest["versions"][0]["id"] == vid


def test_new_version_carries_annotations(hub_ws):
    hub, data, root, did, vid = hub_ws
    hub.put_annotations(vid, str(data / "image_field.fits"),
                        [{"geom": "box", "coords": [10, 10, 40, 40], "label": "耀斑区"}])
    out = hub.new_version(did, vid, "manual", {"note": "另存"})
    assert out["parent"] == vid and out["n_carried"] == 1
    assert len(hub.annotations(out["version_id"])) == 1
    assert [v["op"] for v in hub.versions(did)][-1] == "manual"


# ---------------------------------------------------------------------- 浏览与渲染

def test_scan_without_series_returns_json_safe_null_span(tmp_path, hub_ws):
    """M1 联调报的缺陷：无时序目录的 common_span 是 (-inf, inf) 哨兵，Infinity 不是
    合法 JSON，前端 response.json() 会直接解析失败——对 API 必须出 null。"""
    hub = hub_ws[0]
    p = tmp_path / "img"
    p.mkdir()
    Image.fromarray(np.zeros((64, 64), np.uint8)).save(p / "x.png")
    report = hub.scan(p)
    assert report["t_span"] is None
    json.dumps(report, allow_nan=False)          # 旧行为在这里抛 ValueError


def test_scan_reports_every_modality(hub_ws):
    hub, data, root, did, vid = hub_ws
    report = hub.scan(data)
    assert report["n_items"] >= 3
    for kind in ("image", "cube", "series", "event"):
        assert report["modalities"].get(kind), f"{kind} 应该被认出来"
    assert report["t_span"] and report["elapsed_ms"] >= 0


def test_frame_renders_each_modality(hub_ws):
    hub, data, root, did, vid = hub_ws
    image = hub.frame(str(data / "image_field.fits"), {"width": 320, "height": 200})
    assert image["png"] and not image["cached"]
    assert image["width"] == 320 and image["height"] == 200
    assert image["modality"] == "image"

    cube = hub.frame(str(data / "cube_sim.fits"), {"index": 4, "width": 240, "height": 160})
    assert cube["index"] == 4 and cube["n_views"] > 1
    series = hub.frame(str(data / "telemetry_sim.csv"), {"channel": 1, "width": 400,
                                                        "height": 200})
    assert series["channels"] and series["modality"] == "series"
    events = hub.frame(str(data / "events_sim.json"), {"width": 400, "height": 160})
    assert events["modality"] == "event"


def test_frame_cache_hits_only_for_identical_request(hub_ws):
    hub, data, root, did, vid = hub_ws
    uri = str(data / "cube_sim.fits")
    first = hub.frame(uri, {"index": 2, "width": 200, "height": 120})
    again = hub.frame(uri, {"index": 2, "width": 200, "height": 120})
    other = hub.frame(uri, {"index": 3, "width": 200, "height": 120})
    assert first["cached"] is False and again["cached"] is True
    assert other["cached"] is False and other["index"] == 3
    hub.clear_cache()
    assert hub.frame(uri, {"index": 2, "width": 200, "height": 120})["cached"] is False


def test_thumbnails_survive_a_broken_item(hub_ws):
    hub, data, root, did, vid = hub_ws
    uris = [str(data / "image_field.fits"), str(data / "cube_sim.fits"), str(data / "没了.png")]
    out = hub.thumbnails(uris, cell=(120, 80), workers=3)
    assert out["n_errors"] == 1
    assert len(out["thumbs"]) == 3
    assert all(t["png"] for t in out["thumbs"] if not t.get("error"))
    assert out["workers"] >= 1 and out["elapsed_ms"] >= 0


def test_mosaic_tiles_several_items(hub_ws):
    hub, data, root, did, vid = hub_ws
    out = hub.mosaic([str(data / "image_field.fits"), str(data / "cube_sim.fits")],
                    {"cell_width": 200, "cell_height": 140})
    assert out["png"] and out["n_items"] == 2
    assert out["rows"] >= 1 and out["cols"] >= 1


# ---------------------------------------------------------------------- 标注与审核

def test_annotation_lifecycle(hub_ws):
    hub, data, root, did, vid = hub_ws
    uri = str(data / "image_field.fits")
    written = hub.put_annotations(vid, uri, [
        {"geom": "box", "coords": [20, 30, 80, 90], "label": "耀斑区"},
        {"geom": "point", "coords": [256, 256], "label": "宇宙线"}], role="annotator")
    assert written["n"] == 2 and len(written["ids"]) == 2

    rows = hub.annotations(vid, uri)
    assert {r["state"] for r in rows} == {"draft"}
    first = rows[0]["id"]
    moved = hub.set_state(first, "annotator", "submit")
    assert moved["state"] == "submitted"

    bulk = hub.review_bulk(vid, "reviewer", "approve")
    assert bulk["n"] == 1
    assert hub.queue(vid, "reviewer") == []                      # 审完就没有待办了
    log = hub.review_log(annotation_id=first)
    assert [e["action"] for e in log] == ["submit", "approve"]
    assert hub.stats(did, vid)["states"]["approved"] == 1


def test_replace_refuses_to_overwrite_approved(hub_ws):
    """已通过审核的标注不许被"整条替换"顺手抹掉——那会毁掉审核成果。"""
    hub, data, root, did, vid = hub_ws
    uri = str(data / "image_field.fits")
    hub.put_annotations(vid, uri, [{"geom": "box", "coords": [20, 30, 80, 90],
                                   "label": "耀斑区"}], role="annotator")
    row = hub.annotations(vid, uri)[0]
    hub.set_state(row["id"], "annotator", "submit")
    hub.set_state(row["id"], "reviewer", "approve")

    with pytest.raises(PermissionError):
        hub.put_annotations(vid, uri, [{"geom": "box", "coords": [1, 2, 3, 4], "label": "x"}],
                            role="admin")
    assert [r["state"] for r in hub.annotations(vid, uri)] == ["approved"]


def test_replace_drops_the_previous_draft_and_append_keeps_it(hub_ws):
    hub, data, root, did, vid = hub_ws
    uri = str(data / "image_field.fits")
    hub.put_annotations(vid, uri, [{"geom": "box", "coords": [20, 30, 80, 90],
                                   "label": "耀斑区"}], role="annotator")
    replaced = hub.put_annotations(vid, uri, [{"geom": "box", "coords": [5, 5, 15, 15],
                                              "label": "耀斑区"}], role="admin")
    assert replaced["n"] == 1 and replaced["removed"] == 1
    assert [r["ann"]["coords"] for r in hub.annotations(vid, uri)] == [[5.0, 5.0, 15.0, 15.0]]

    appended = hub.put_annotations(vid, uri, [{"geom": "point", "coords": [7, 8],
                                              "label": "宇宙线"}], role="admin", replace=False)
    assert appended["removed"] == 0
    assert hub.stats(did, vid)["n_annotations"] == 2


def test_progress_and_diff_and_lineage(hub_ws):
    hub, data, root, did, vid = hub_ws
    child = hub.new_version(did, vid, "manual")["version_id"]
    hub.put_annotations(child, str(data / "cube_sim.fits"),
                        [{"geom": "box", "coords": [1, 2, 20, 30], "label": "宇宙线"}])
    progress = hub.progress(child)
    assert progress["total"] == 1 and progress["by_state"]["draft"] == 1
    assert progress["ratio"] == 0.0
    diff = hub.diff(vid, child)
    assert diff["n_added"] == 1 and diff["n_common"] == 0
    assert [v["id"] for v in hub.lineage(child)][-1] == child


def test_items_and_stats_cover_the_dataset(hub_ws):
    hub, data, root, did, vid = hub_ws
    hub.put_annotations(vid, str(data / "image_field.fits"),
                        [{"geom": "box", "coords": [20, 30, 80, 90], "label": "耀斑区"}],
                        role="annotator")
    rows = hub.items(did, vid)
    assert rows and all("uri" in r and "states" in r for r in rows)
    assert any(r["n_annotations"] == 1 for r in rows)
    series_only = hub.items(did, vid, modality="series")
    assert all(r["modality"] == "series" for r in series_only)
    stats = hub.stats(did, vid)
    assert stats["n_annotations"] == 1 and stats["n_items_on_disk"] >= 3
    assert stats["sources"] == {"manual": 1} and 0 < stats["labeled_ratio"] < 1
    assert 0 <= stats["labeled_ratio"] <= 1


def test_items_are_disk_union_annotated_in_every_state(hub_ws):
    """P1 回归：列表 = 磁盘全集 ∪ 已标注。

    旧实现是 `sorted(grouped) or 磁盘`——只要落了一条标注，没标过的文件就从
    管理台消失，用户不知道还剩多少活。三种状态逐一钉死：
    零标注 = 磁盘全集；部分标注 = 全集且计数正确；全部标注 = 全集不缩水。
    """
    hub, data, root, did, vid = hub_ws
    disk = {r["uri"] for r in hub.items(did, vid)}
    assert disk and all(r["n_annotations"] == 0 for r in hub.items(did, vid))

    # D3：items() 与磁盘扫描现在统一给相对工作区根的 uri，比对口径随之取相对
    target = hub.store.to_rel(str(data / "image_field.fits"))
    hub.put_annotations(vid, target,
                        [{"geom": "box", "coords": [20, 30, 80, 90], "label": "耀斑区"}],
                        role="annotator")
    rows = {r["uri"]: r for r in hub.items(did, vid)}
    assert set(rows) == disk, "落一条标注不许把未标注文件挤出列表"
    assert rows[target]["n_annotations"] == 1
    assert sum(1 for r in rows.values() if r["n_annotations"] == 0) == len(disk) - 1

    for uri in sorted(disk - {target}):
        hub.put_annotations(vid, uri, [{"geom": "point", "coords": [1, 2], "label": "宇宙线"}],
                            role="annotator")
    rows = {r["uri"]: r for r in hub.items(did, vid)}
    assert set(rows) == disk and all(r["n_annotations"] >= 1 for r in rows.values())


def test_items_keep_annotated_entry_whose_file_disappeared(hub_ws, tmp_path):
    """已标注但磁盘上没了的条目要留在列表里（sniff 读不了内容就退回扩展名）。"""
    hub, data, root, did, vid = hub_ws
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    csv_path = lonely / "telemetry.csv"
    csv_path.write_text("t,v\n0,1\n1,2\n2,3\n", encoding="utf-8")
    created = hub.create_dataset("消失条目", "detection", lonely)
    d2, v2 = created["dataset_id"], created["version_id"]
    hub.put_annotations(v2, str(csv_path),
                        [{"geom": "interval", "coords": [0, 1], "label": "段"}],
                        role="annotator")
    csv_path.unlink()
    hub._invalidate_scans()
    rows = hub.items(d2, v2)
    assert len(rows) == 1 and rows[0]["n_annotations"] == 1
    assert rows[0]["modality"] == ""      # 歧义扩展名没了内容无从判断，如实标 unknown


def test_load_item_caches_until_the_file_changes(hub_ws):
    """P0-A 回归：同 uri 且磁盘指纹未变时复用解析结果；文件一变必须重解析。"""
    hub, data, root, did, vid = hub_ws
    uri = str(data / "telemetry_sim.csv")
    first = hub.load_item(uri)
    assert hub.load_item(uri) is first                 # 换通道/换窗口不再重解析
    hub.frame(uri, {"channel": 0, "width": 320, "height": 160})
    hub.frame(uri, {"channel": 1, "width": 320, "height": 160})
    assert hub.load_item(uri) is first                 # frame 穿透帧缓存时也走同一份

    Path(uri).touch()                                  # mtime 变 → 指纹失效
    assert hub.load_item(uri) is not first

    ghost = str(data / "根本没有这个文件.csv")
    with pytest.raises(Exception):
        hub.load_item(ghost)                           # 异常路径不进缓存、原样上抛


def test_scan_cache_reuses_until_invalidated(hub_ws):
    hub, data, root, did, vid = hub_ws
    a = hub._scan_items(data)
    assert hub._scan_items(data) is a                  # TTL 内同一目录不重扫
    hub._invalidate_scans()
    c = hub._scan_items(data)
    assert c is not a and len(c) == len(a)


# ---------------------------------------------------------------------- 分析与后端

def test_analyze_returns_metrics_and_geometry(hub_ws):
    hub, data, root, did, vid = hub_ws
    out = hub.analyze(str(data / "image_field.fits"), ["star_detect", "bg_stats"])
    assert out["metrics"], "真值星图必然能算出指标"
    assert any(k.startswith("star_detect.") for k in out["metrics"])
    assert out["anns"] and {"geom", "coords"} <= set(out["anns"][0])
    assert out["errors"] == {}


def test_analyze_reports_unknown_and_wrong_modality(hub_ws):
    hub, data, root, did, vid = hub_ws
    out = hub.analyze(str(data / "telemetry_sim.csv"), ["没有这个算子", "star_detect"])
    assert set(out["errors"]) == {"没有这个算子", "star_detect"}
    assert out["metrics"] == {}


def test_probe_and_backends_catalog(hub_ws):
    hub, data, root, did, vid = hub_ws
    probe = hub.probe()
    assert probe["tier"] in ("high", "mid", "low")
    assert probe["cpu_count"] >= 1 and probe["ram_gb"] > 0
    assert "cosmic_reject" in probe["registered"]
    catalog = hub.backends(domain="aerospace", modality="image")
    assert catalog and all({"available", "reason", "params"} <= set(b) for b in catalog)
    assert any(b["name"] == "cosmic_reject" and b["available"] for b in catalog)
    assert hub.backends(task="series_anomaly")


def test_gate_uses_measured_numbers(hub_ws):
    hub, data, root, did, vid = hub_ws
    blocked = hub.gate_check("dynamic_threshold", "series_anomaly", "aerospace", 0.90)
    assert blocked["usable"] is False
    assert "实测" in blocked["reason"]          # 没测过的后端不许直接上岗，理由要说清
    assert blocked["threshold"] == 0.90
    assert hub.recommend("aerospace", "reject_cosmic", k=3)


# ---------------------------------------------------------------------- 智能标注

def test_same_image_and_undo(hub_ws):
    hub, data, root, did, vid = hub_ws
    truth = json.loads((data / "truth.json").read_text(encoding="utf-8"))
    uri = str(data / "image_field.fits")
    arr = np.asarray(io.load_any(uri).load())
    ys, xs = np.nonzero(arr > np.percentile(arr, 99.9))
    prompt = {"kind": "box", "coords": [int(xs.mean()) - 8, int(ys.mean()) - 8,
                                       int(xs.mean()) + 8, int(ys.mean()) + 8],
              "label": "耀斑区", "source_uri": uri}
    out = hub.same_image(uri, prompt)
    assert out["n"] >= 1 and out["anns"][0]["geom"] == "box"
    assert truth["image_truth_sources"] >= 1


def test_preannotate_then_undo(hub_ws):
    hub, data, root, did, vid = hub_ws
    uri = str(data / "image_field.fits")
    hub.put_annotations(vid, uri, [{"geom": "box", "coords": [20, 30, 80, 90],
                                   "label": "人工耀斑"}], role="annotator")
    params = {"backend": "cosmic_reject", "backend_params": {"nsigma": 3.0, "size": 3},
              "workers": 1}
    dry = hub.preannotate([uri], params)
    assert dry["n_items"] == 1 and dry["n_annotations"] >= 1
    applied = hub.preannotate([uri], params, apply_to=vid)
    assert applied["version_id"] != vid
    assert applied["n_written"] == dry["n_annotations"]          # 机器结果落到新版本

    assert [r["ann"]["label"] for r in hub.annotations(vid)] == ["人工耀斑"]   # 父版本没被改
    child_rows = [r for r in hub.annotations(applied["version_id"])
                  if r["ann"]["source"] == "manual"]
    assert len(child_rows) == 1                                  # 人工那条被继承过来
    undone = hub.undo_machine(applied["version_id"])
    assert undone["from"] == applied["version_id"] and undone["to"] == vid


def test_speedup_and_hard_examples(hub_ws):
    hub, data, root, did, vid = hub_ws
    uris = [str(data / "image_field.fits"), str(data / "cube_sim.fits")]
    speed = hub.speedup(uris, {"backend": "cosmic_reject", "workers": 2,
                              "backend_params": {"nsigma": 3.0}})
    assert speed["n_items"] == 2 and speed["consistent"] in (True, False)
    hard = hub.hard_examples(vid, uris, {"backend": "cosmic_reject", "k": 5,
                                        "backend_params": {"nsigma": 3.0}})
    assert hard["k"] == 5 and isinstance(hard["queue"], list)


def test_active_round_ranks_by_uncertainty(hub_ws):
    hub, data, root, did, vid = hub_ws
    uris = [str(data / "telemetry_sim.csv")]
    out = hub.active_round(vid, uris, {"backend": "dynamic_threshold", "budget": 1,
                                      "backend_params": {"nsigma": 3.0}})
    assert out["budget"] == 1 and out["chosen"]
    # D3：API 负载里的 uri 是相对工作区根的形式（前端原样回传即可命中）
    assert out["ranked"][0]["uri"] == hub.store.to_rel(uris[0])
    assert isinstance(out["advice"], str)


# ---------------------------------------------------------------------- 清洗 / 增强

def test_clean_preview_apply_dropped_rollback(hub_ws, tmp_path):
    hub, data, root, did, vid = hub_ws
    scene = str(data / "image_field.fits")
    dup = str(tmp_path / "copy.fits")
    Path(dup).write_bytes(Path(scene).read_bytes())        # 逐字节重复，dedup 必然成组
    specs = [{"name": "dedup"}]
    assert hub.clean_preview([scene, dup], specs)["n_dropped"] == 1
    report = hub.clean_apply(vid, specs, uris=[scene, dup])
    assert report["summary"]["n_kept"] == 1
    assert len(hub.clean_dropped(report["version_id"])) == 1
    back = hub.clean_rollback(report["version_id"])
    assert back["restored"] == vid and back["removed_files"] == []   # 没写文件，也就没什么可删
    assert Path(scene).is_file() and Path(dup).is_file()             # 原件都在


def test_augment_check_run_and_replay(hub_ws):
    hub, data, root, did, vid = hub_ws
    scene = str(data / "image_field.fits")
    hub.put_annotations(vid, scene, [{"geom": "box", "coords": [20, 30, 80, 90],
                                     "label": "耀斑区"}], role="annotator")
    recipe = {"seed": 2, "factor": 1, "ops": [{"name": "flip",
                                              "params": {"p_horizontal": 1.0}}]}
    assert hub.augment_check([scene], recipe, category="aerospace")["warnings"] == []
    report = hub.augment_run(vid, recipe, uris=[scene])
    assert report["summary"]["n_generated"] == 1
    assert hub.augment_replay(report["version_id"])["identical"] is True
    assert hub.annotations(report["version_id"])[-1]["uri"].endswith(".png")


def test_augment_check_reuses_the_load_lru(tmp_path, monkeypatch):
    """REQ-W12 回归：同参数第二次 augment/check 不许再逐条重解析文件。

    前端实测冷 6098ms/热 6081ms——LRU 没覆盖到这条逐条 load_any 的路。
    这里给一个全新 Hub（独立缓存），用计数包装 io.load_any 来证明：
    首轮逐条解析、热轮零调用、文件一变必须重新解析。
    """
    pytest.importorskip("astropy")
    data = synth.make_dataset(tmp_path / "aerospace", n_telemetry=120, seed=5)
    hub = Hub(tmp_path / "mdva.sqlite", out_dir=tmp_path / "out")
    try:
        uris = [str(data / "image_field.fits"), str(data / "telemetry_sim.csv")]
        recipe = {"seed": 0, "factor": 1, "ops": [{"name": "flip",
                                                  "params": {"p_horizontal": 1.0}}]}
        calls: list = []
        real = io.load_any
        monkeypatch.setattr(io, "load_any",
                            lambda uri, **kw: (calls.append(str(uri)), real(uri, **kw))[1])

        first = hub.augment_check(uris, recipe, category="aerospace")
        assert len(calls) == len(uris)                   # 冷态：逐条解析
        calls.clear()
        second = hub.augment_check(uris, recipe, category="aerospace")
        assert calls == [] and second == first           # 热态：零重解析，结论一致
        Path(uris[1]).touch()                            # 文件一变指纹失效，必须重解析
        calls.clear()
        hub.augment_check(uris, recipe, category="aerospace")
        assert len(calls) == 1 and Path(calls[0]).samefile(uris[1])
    finally:
        hub.close()


def test_operators_describe_every_layer(hub_ws):
    hub, data, root, did, vid = hub_ws
    for kind in ("analysis", "clean", "export"):
        rows = hub.operators(kind)
        assert rows and {"name", "description", "schema"} <= set(rows[0])
    ops, presets = hub.operators("augment")
    assert {"name", "schema"} <= set(ops[0]) and presets[0]["reason"]
    with pytest.raises(ValueError):
        hub.operators("nope")


# ---------------------------------------------------------------------- 导出

def test_export_and_validate_round_trip(hub_ws):
    hub, data, root, did, vid = hub_ws
    hub.put_annotations(vid, str(data / "image_field.fits"),
                        [{"geom": "box", "coords": [20, 30, 80, 90], "label": "耀斑区"},
                         {"geom": "box", "coords": [100, 120, 160, 170], "label": "宇宙线"}],
                        role="annotator", state="approved")
    report = hub.export(vid, "coco", {"split_ratios": "0.6/0.2/0.2", "seed": 1})
    assert report["out_dir"] and report["n_annotations"] >= 1
    check = hub.export_validate(vid, report["out_dir"])
    assert check["ok"] is True, check["summary"]
    assert hub.export_report(vid)


def test_export_writes_training_scripts(hub_ws):
    hub, data, root, did, vid = hub_ws
    hub.put_annotations(vid, str(data / "image_field.fits"),
                        [{"geom": "box", "coords": [20, 30, 80, 90], "label": "耀斑区"}],
                        role="annotator")
    report = hub.export(vid, "yolo", {}, make_scripts=True)
    # D3：负载里的 out_dir 已相对化，读产物前先还原（原样回传 export_validate 已由上一用例覆盖）
    out = Path(hub.store.to_abs(report["out_dir"]))
    assert (out / "train.py").is_file() and (out / "DATASET.md").is_file()
    assert "https://" not in (out / "train.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------- 收录

def test_collect_copy_then_undo(hub_ws, tmp_path):
    hub, data, root, did, vid = hub_ws
    source = data / "image_field.fits"
    target = tmp_path / "收进来"
    out = hub.collect([str(source)], target)
    assert out["n_entries"] == 1 and out["manifest"]
    landed = target / source.parent.name / source.name
    assert landed.is_file(), "缺省按来源目录落位，多模型同名输出才不会互相覆盖"

    undone = hub.collect_undo(out["manifest"])
    assert undone["removed"] == 1 and undone["refused"] == 0
    assert not landed.exists()
    assert source.is_file()                                      # 原件永远不动


def test_collect_latest_manifest(hub_ws, tmp_path):
    hub, data, root, did, vid = hub_ws
    target = tmp_path / "清单目录"
    made = hub.collect([str(data / "cube_sim.fits")], target)
    found = hub.collect_latest(target)
    assert found["exists"] and found["manifest"] == made["manifest"]
