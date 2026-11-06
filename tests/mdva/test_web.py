"""HTTP 层：路由表、参数解析、状态码映射，以及真 socket 上的往返。

`routes.call()` 不建 socket，所以这些断言与用哪个 Web 框架无关（本项目用的是标准库）。
最后一段起真服务器：浏览器拿到的 PNG、缓存头、SPA 回退、越界路径都必须当场验一遍，
不然"双击即用"这句话就是空的。
"""

import json
import threading
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pytest

from mdva import io, synth
from mdva.web import routes
from mdva.web.deps import Workspaces, discover
from mdva.web.routes import Bad, Params, call
from mdva.web.server import MAX_BODY, STATIC_DIR, is_loopback, make_server


# ---------------------------------------------------------------------- 夹具

@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    """一个工作区根目录：里面只有数据，库由第一次 POST /api/datasets 当场建起来。"""
    pytest.importorskip("astropy")
    root = tmp_path_factory.mktemp("webroot")
    data = synth.make_dataset(root / "aerospace", n_telemetry=1200, seed=7)
    ws = Workspaces(root)
    res = call(ws, "POST", "/api/datasets", None,
               {"name": "航天标注", "type": "detection", "root": str(data),
                "category": "aerospace", "labels": ["耀斑区", "宇宙线"]})
    assert res.status == 200, res.json_body
    yield ws, data, root, res.json_body["dataset_id"], res.json_body["version_id"]
    ws.close()


def get(workspaces, path, **query):
    res = call(workspaces, "GET", "/api" + path, query, None)
    assert res.status == 200, (path, res.json_body)
    return res.json_body


def post(workspaces, path, body=None):
    res = call(workspaces, "POST", "/api" + path, None, body or {})
    assert res.status == 200, (path, res.json_body)
    return res.json_body


# ---------------------------------------------------------------------- 参数解析

def test_params_reads_query_and_body_alike():
    p = Params({"width": "640", "invert": "true", "uris": json.dumps(["a.fits", "b.fits"])},
               {"width": 800, "uris": ["c.fits"], "cell": [196, 122], "workers": 4})
    assert p.int_("width") == 800                 # 请求体优先，查询串当兜底
    assert p.flag("invert") is True and p.int_("workers") == 4
    assert p.list_("uris") == ["c.fits"]
    assert Params({"uris": "a.fits,b.fits"}).list_("uris") == ["a.fits", "b.fits"]
    assert p.pair("cell") == (196, 122)
    assert Params({"cell": "320x200"}).pair("cell") == (320, 200)


def test_params_rejects_garbage_with_bad():
    with pytest.raises(Bad):
        Params({"width": "abc"}).int_("width")
    with pytest.raises(Bad):
        Params({}).need("uri")
    with pytest.raises(Bad):
        Params({"cell": "320"}).pair("cell")
    with pytest.raises(Bad):
        Params({"uris": "[1,"}).list_("uris")
    assert Params({"x": ""}).list_("x") == []


# ---------------------------------------------------------------------- 路由与状态码

def test_index_advertises_every_route(stack):
    ws = stack[0]
    index = call(ws, "GET", "/api").json_body
    assert len(index["endpoints"]) == len(routes.paths())
    assert all(row["doc"] for row in index["endpoints"])       # 每个端点都要有一句话说明
    assert {row["path"] for row in index["endpoints"]} >= {"/api/health", "/api/frame",
                                                          "/api/annotations"}


def test_unknown_path_and_wrong_method(stack):
    ws = stack[0]
    assert call(ws, "GET", "/api/没有这个接口").status == 404
    res = call(ws, "POST", "/api/items")
    assert res.status == 405 and "GET" in res.json_body["error"]   # 说清支持哪个方法
    assert call(ws, "GET", "/health").status == 404             # 忘了 /api 前缀也不给接口


def test_errors_carry_a_readable_message(stack, tmp_path):
    ws, data, root, did, vid = stack
    ghost = tmp_path / "根本不存在.fits"
    bad = call(ws, "GET", "/api/frame", {"uri": str(ghost)})
    assert bad.status == 404 and ghost.name in bad.json_body["error"]
    missing = call(ws, "POST", "/api/annotations", None, {"version": vid})
    assert missing.status == 400 and "uri" in missing.json_body["error"]
    assert call(ws, "GET", "/api/dataset", {"dataset": "ds没有这个"}).status == 404


def test_role_violation_is_403(stack):
    ws, data, root, did, vid = stack
    created = post(ws, "/annotations", {"version": vid, "uri": str(data / "image_field.fits"),
                                       "anns": [{"geom": "box", "coords": [10, 10, 40, 40],
                                                 "label": "耀斑区"}], "role": "annotator"})
    res = call(ws, "POST", "/api/annotations/state", None,
               {"annotation_id": created["ids"][0], "role": "annotator", "action": "approve"})
    assert res.status == 403                       # 标注员不能自己验收，越权必须是 403
    assert "标注员" in res.json_body["error"]


# ---------------------------------------------------------------------- 元信息

def test_health_and_probe(stack):
    ws, data, root, did, vid = stack
    health = get(ws, "/health")
    assert health["ok"] and health["offline"] is True
    assert health["root"] == str(root.resolve())
    probe = get(ws, "/probe")
    assert probe["tier"] in ("high", "mid", "low") and "cosmic_reject" in probe["registered"]
    ops = get(ws, "/operators", kind="export")
    assert {"name", "schema"} <= set(ops["ops"][0])
    recipes = get(ws, "/augment/recipes")
    assert recipes["presets"][0]["recipe"]["ops"] and recipes["task_presets"]


def test_workspaces_route_only_accepts_names(stack, tmp_path):
    """工作区只认扫描出来的名字：路径永远来自启动参数，不来自请求。"""
    from mdva.web.services import Hub

    ws, data, root, did, vid = stack
    for attempt in ("../etc", str(tmp_path), root.name):
        assert call(ws, "GET", "/api/health", {"ws": attempt}).status == 404

    lab = root / "lab"
    lab.mkdir(exist_ok=True)
    hub = Hub(lab / "mdva.sqlite")
    hub.create_dataset("实验室", "detection", lab)
    hub.close()
    rows = post(ws, "/workspaces/rescan")["workspaces"]
    assert {r["ws"] for r in rows} >= {".", "lab"}
    listed = {w["ws"]: w["n_datasets"] for w in get(ws, "/workspaces")["workspaces"]}
    assert listed["lab"] == 1 and listed["."] >= 1
    assert get(ws, "/health", ws="lab")["out_dir"].startswith(str(lab))
    assert discover(root)[0]["ws"] == "."           # 根目录永远是第一个工作区


# ---------------------------------------------------------------------- 数据集与条目

def test_fresh_dataset_lists_disk_items_and_marks_progress(stack):
    """没标过的条目也要出现在列表里（否则用户以为数据丢了），但要清楚标成 0 条。"""
    ws, data, root, did, vid = stack
    fresh = post(ws, "/datasets", {"name": "空集", "type": "detection", "root": str(data),
                                   "labels": ["耀斑区"]})
    items = get(ws, "/items", dataset=fresh["dataset_id"])["items"]
    assert items and all(row["n_annotations"] == 0 for row in items)
    post(ws, "/annotations", {"version": fresh["version_id"],
                              "uri": str(data / "image_field.fits"),
                              "anns": [{"geom": "box", "coords": [10, 10, 40, 40],
                                        "label": "耀斑区"}], "role": "annotator"})
    after = get(ws, "/items", dataset=fresh["dataset_id"])["items"]
    rows = {row["uri"]: row for row in after}
    # 列表 = 磁盘 ∪ 已标注：落一条标注不许把未标注文件挤出管理台
    assert len(after) == len(items)
    assert sum(1 for r in after if r["n_annotations"] == 0) == len(items) - 1
    # D3：绝对 uri 入库、列表原样回传的是相对工作区根的 posix 路径（前端拿到什么回什么）
    row = rows[(data / "image_field.fits").relative_to(root).as_posix()]
    assert row["n_annotations"] == 1 and row["states"]["draft"] == 1
    assert row["machine_only"] is False and row["labels"] == ["耀斑区"]
    stats = get(ws, "/stats", dataset=fresh["dataset_id"])
    assert stats["n_items"] == 1 and stats["n_items_on_disk"] == len(items)
    assert stats["labels"] == {"耀斑区": 1}


def test_dataset_manifest_and_scan(stack):
    ws, data, root, did, vid = stack
    ds = get(ws, "/dataset", dataset=did)
    assert ds["name"] == "航天标注"
    assert [l["name"] for l in ds["labels"]] == ["耀斑区", "宇宙线"]
    disk = get(ws, "/scan", root=str(data))
    assert disk["n_items"] >= 3 and disk["modalities"]["image"] >= 1
    man = get(ws, "/manifest", dataset=did)
    assert man["dataset_id"] == did and man["versions"]
    assert Path(data / "manifest.json").is_file()               # 清单真的落盘了


def test_labels_replace_needs_a_full_table(stack):
    ws, did = stack[0], stack[3]
    rows = post(ws, "/labels", {"dataset": did, "labels": ["新类别"]})
    assert [l["name"] for l in rows["labels"]] == ["新类别"]
    assert call(ws, "POST", "/api/labels", None, {"dataset": did, "labels": []}).status == 400
    post(ws, "/labels", {"dataset": did, "labels": ["耀斑区", "宇宙线"]})   # 还原给后面的用例


def test_dataset_type_is_validated(stack, tmp_path):
    ws = stack[0]
    res = call(ws, "POST", "/api/datasets", None, {"name": "坏类型", "type": "nope",
                                                   "root": str(tmp_path)})
    assert res.status == 400 and "未知数据集类型" in res.json_body["error"]


# ---------------------------------------------------------------------- 出图

def test_frame_returns_png_and_canvas_mapping(stack):
    ws, data, root, did, vid = stack
    uri = str(data / "image_field.fits")
    res = call(ws, "GET", "/api/frame", {"uri": uri, "width": 320, "height": 200})
    assert res.status == 200 and res.data.startswith(b"\x89PNG")
    assert res.headers["X-Mdva-Modality"] == "image"
    assert res.headers["X-Mdva-Cached"] == "0"                 # 首帧真的渲染过
    again = call(ws, "GET", "/api/frame", {"uri": uri, "width": 320, "height": 200})
    assert again.headers["X-Mdva-Cached"] == "1"               # 拖动时同一帧不再重渲染

    shape = [i for i in get(ws, "/scan", root=str(data))["items"] if i["uri"] == uri][0]["shape"]
    meta = get(ws, "/frame/meta", uri=uri, width=320, height=200)
    fit = meta["info"]["fit"]                                   # (x0, y0, w, h, 缩放)
    scale = min(320 / shape[-1], 200 / shape[-2])
    assert meta["png_bytes"] > 0 and meta["width"] == 320
    assert fit[-1] == pytest.approx(scale)                      # 画布缩放与 L2 一致
    assert fit[2] == round(shape[-1] * scale) and fit[3] == round(shape[-2] * scale)
    assert fit[0] + fit[2] <= 320 and fit[1] + fit[3] <= 200     # 落在画布内，标注才不会画歪


def test_series_frame_thumbnails_and_mosaic(stack):
    ws, data, root, did, vid = stack
    body = get(ws, "/frame/meta", uri=str(data / "telemetry_sim.csv"),
               width=600, height=240, index=1)
    assert body["modality"] == "series" and body["channels"]
    thumbs = get(ws, "/thumbs", uris=f"{data / 'image_field.fits'},{data / 'cube_sim.fits'}",
                 cell="120x90")
    assert thumbs["n_errors"] == 0 and len(thumbs["thumbs"]) == 2
    mosaic = call(ws, "GET", "/api/mosaic", {"uris": str(data / "image_field.fits"),
                                             "cell": "96x96"})
    assert mosaic.status == 200 and mosaic.data.startswith(b"\x89PNG")


def test_analyze_reports_and_overlays(stack):
    ws, data, root, did, vid = stack
    out = post(ws, "/analyze", {"uri": str(data / "image_field.fits"),
                                "analyzers": ["cosmic_reject", "bg_stats"],
                                "params": {"analyzer_params": {"nsigma": 3.0}}})
    assert out["metrics"] and out["errors"] == {}
    assert all({"geom", "coords"} <= set(a) for a in out["anns"])
    unknown = post(ws, "/analyze", {"uri": str(data / "image_field.fits"),
                                    "analyzers": ["没有这个算子"]})
    assert "没有这个算子" in unknown["errors"]


# ---------------------------------------------------------------------- 智能标注

def test_preannotate_apply_and_undo_over_http(stack):
    ws, data, root, did, vid = stack
    uri = str(data / "cube_sim.fits")
    post(ws, "/annotations", {"version": vid, "uri": uri,
                              "anns": [{"geom": "box", "coords": [5, 5, 30, 30],
                                        "label": "耀斑区"}], "role": "annotator"})
    body = {"uris": [uri], "backend": "cosmic_reject",
            "backend_params": {"nsigma": 3.0, "size": 3}, "workers": 2}
    dry = post(ws, "/smart/preannotate", body)
    assert dry["n_items"] == 1 and dry["n_annotations"] >= 1
    assert "version_id" not in dry                              # 干跑绝不落库

    applied = post(ws, "/smart/apply", {**body, "version": vid})
    assert applied["version_id"] != vid and applied["n_written"] == dry["n_annotations"]
    assert applied["carried"] >= 1                              # 人工框跟着进新版本，没缩水
    rows = get(ws, "/annotations", version=applied["version_id"])["annotations"]
    assert any(r["ann"]["source"] == "manual" for r in rows)
    speed = call(ws, "GET", "/api/smart/speedup",
                 {"uris": f"{uri},{data / 'image_field.fits'}", "backend": "cosmic_reject",
                  "workers": 2})
    assert speed.status == 200 and speed.json_body["n_items"] == 2
    assert speed.json_body["workers"] == 2
    hard = get(ws, "/smart/hard", version=vid, uris=uri, backend="cosmic_reject", k=3)
    assert hard["k"] == 3
    undone = post(ws, "/smart/undo", {"version": applied["version_id"]})
    assert undone["from"] == applied["version_id"] and undone["to"] == vid


def test_same_image_prompt_and_active_round(stack):
    ws, data, root, did, vid = stack
    uri = str(data / "image_field.fits")
    arr = np.asarray(io.load_any(uri).load())
    ys, xs = np.nonzero(arr > np.percentile(arr, 99.9))
    same = post(ws, "/smart/same_image", {"uri": uri, "kind": "box", "label": "耀斑区",
                                          "source_uri": uri,
                                          "coords": [int(xs.mean()) - 8, int(ys.mean()) - 8,
                                                     int(xs.mean()) + 8, int(ys.mean()) + 8]})
    assert same["n"] >= 1 and same["anns"][0]["geom"] == "box"
    round_ = post(ws, "/smart/active_round",
                  {"version": vid, "uris": [str(data / "telemetry_sim.csv")],
                   "backend": "dynamic_threshold", "budget": 1,
                   "backend_params": {"nsigma": 3.0}})
    assert round_["budget"] == 1 and round_["chosen"]
    assert isinstance(get(ws, "/smart/history", version=vid)["rounds"], list)


# ---------------------------------------------------------------------- 后端与门槛

def test_backend_catalog_gate_and_recommend(stack):
    ws, data, root, did, vid = stack
    rows = get(ws, "/backends", domain="aerospace", modality="image")["backends"]
    assert rows and all({"available", "reason", "params"} <= set(b) for b in rows)
    assert any(b["name"] == "cosmic_reject" and b["available"] for b in rows)
    assert get(ws, "/backends", task="series_anomaly")["n"] >= 3      # 只按任务筛也要有结果
    gate = get(ws, "/gate", backend="dynamic_threshold", task="series_anomaly",
               domain="aerospace", threshold="0.9")
    assert gate["usable"] is False and "实测" in gate["reason"]
    rec = get(ws, "/recommend", domain="aerospace", task="reject_cosmic", k="2")
    assert rec["has_table"] is False and rec["recommended"]      # 没实测表也能按静态分推荐
    assert all("reasons" in item for item in rec["recommended"])  # 每条推荐都说得出理由
    assert get(ws, "/bench")["rows"] == []               # 还没跑过实测：表是空的
    assert "实测" in gate["reason"]


# ---------------------------------------------------------------------- 清洗 / 增强

def test_clean_preview_apply_dropped_rollback(stack, tmp_path):
    ws, data, root, did, vid = stack
    scene = str(data / "image_field.fits")
    dup = tmp_path / "副本.fits"
    dup.write_bytes(Path(scene).read_bytes())
    specs = [{"name": "dedup"}]
    assert post(ws, "/clean/preview", {"uris": [scene, str(dup)],
                                       "specs": specs})["n_dropped"] == 1
    applied = post(ws, "/clean/apply", {"version": vid, "uris": [scene, str(dup)],
                                        "specs": specs})
    assert applied["summary"]["n_kept"] == 1
    assert len(get(ws, "/clean/dropped", version=applied["version_id"])["dropped"]) == 1
    assert post(ws, "/clean/rollback", {"version": applied["version_id"]})["restored"] == vid
    assert Path(scene).is_file() and dup.is_file()              # 原件一条都没少


def test_augment_recipe_check_run_lineage_replay(stack):
    ws, data, root, did, vid = stack
    scene = str(data / "image_field.fits")
    recipe = {"seed": 3, "factor": 1, "ops": [{"name": "flip",
                                               "params": {"p_horizontal": 1.0}}]}
    assert post(ws, "/augment/check", {"uris": [scene], "recipe": recipe,
                                       "category": "aerospace"})["warnings"] == []
    warn = post(ws, "/augment/check", {"uris": [scene], "category": "aerospace",
                                       "recipe": {"seed": 1, "factor": 1,
                                                  "ops": [{"name": "flip", "params":
                                                           {"p_vertical": 1.0}}]}})
    assert any("镜像" in w for w in warn["warnings"])            # 航天数据上下翻该提醒
    report = post(ws, "/augment/run", {"version": vid, "recipe": recipe, "uris": [scene],
                                       "author": "徐靖宇"})
    assert report["summary"]["n_generated"] == 1
    lineage = get(ws, "/augment/lineage", version=report["version_id"])["lineage"]
    # D3：谱系负载里的 uri/source 已相对化（前端原样展示/回传即可）
    assert lineage[0]["source"] == Path(scene).relative_to(root).as_posix() and lineage[0]["chain"]
    assert post(ws, "/augment/replay", {"version": report["version_id"]})["identical"] is True
    assert post(ws, "/augment/suggest", {"dataset": did})["recipe"]["ops"]


# ---------------------------------------------------------------------- 导出 / 收录

def test_export_validate_and_report(stack):
    ws, data, root, did, vid = stack
    post(ws, "/annotations", {"version": vid, "uri": str(data / "cube_sim.fits"),
                              "anns": [{"geom": "box", "coords": [8, 8, 48, 40],
                                        "label": "耀斑区"}], "role": "annotator",
                              "state": "approved"})
    formats = get(ws, "/export/formats")
    assert {"name", "schema"} <= set(formats["formats"][0])
    report = post(ws, "/export", {"version": vid, "format": "coco",
                                  "params": {"split_ratios": "0.6/0.2/0.2", "seed": 1}})
    # D3：out_dir 相对工作区根返回；下面 export_validate 原样回传该相对值（往返即契约）
    assert report["n_annotations"] >= 1 and Path(root, report["out_dir"]).is_dir()
    check = post(ws, "/export/validate", {"version": vid, "dir": report["out_dir"]})
    assert check["ok"] is True, check["summary"]
    assert get(ws, "/export/report", version=vid)["reports"]
    assert get(ws, "/tasks", kind="export")["n"] >= 1


def test_collect_copy_and_undo(stack, tmp_path):
    ws, data, root, did, vid = stack
    target = tmp_path / "收录到这里"
    got = post(ws, "/collect", {"uris": [str(data / "image_field.fits")],
                                "target": str(target), "mode": "copy"})
    assert got["n_entries"] == 1 and Path(got["manifest"]).is_file()
    assert list(target.rglob("*.fits"))
    assert get(ws, "/collect/latest", target=str(target))["exists"] is True
    assert post(ws, "/collect/undo", {"manifest": got["manifest"]})["removed"] >= 1
    assert not list(target.rglob("*.fits"))


def test_version_lineage_diff_and_queue(stack):
    ws, data, root, did, vid = stack
    created = post(ws, "/version", {"dataset": did, "op": "manual"})
    fresh, parent = created["version_id"], created["parent"]
    assert [v["op"] for v in get(ws, "/versions", dataset=did)["versions"]][-1] == "manual"
    lineage = get(ws, "/lineage", version=fresh)["lineage"]
    assert lineage[-1]["id"] == fresh and lineage[0]["parent_id"] is None   # 从根排到当前
    diff = get(ws, "/diff", v1=parent, v2=fresh)
    assert diff["n_added"] == 0 and diff["n_removed"] == 0      # carry 把父版本全带过来了
    assert get(ws, "/progress", version=vid)["total"] >= 1
    assert isinstance(get(ws, "/review_log", version=vid)["log"], list)
    assert get(ws, "/queue", version=vid, role="reviewer")["queue"] == []   # 没人提交，队列空
    draft = str(data / "image_field.fits")          # 这条还是 draft（上面导出用的那条已 approved）
    submitted = post(ws, "/annotations/submit", {"version": vid, "uri": draft,
                                                 "role": "annotator"})
    assert submitted["n_submitted"] >= 1
    # D3：待办队列负载给相对 uri（绝对 draft 作为入参仍可命中，见上一行）
    assert Path(draft).relative_to(root).as_posix() in get(ws, "/queue", version=vid,
                                                           role="reviewer")["queue"]
    bulk = post(ws, "/annotations/review", {"version": vid, "role": "reviewer",
                                           "action": "approve"})
    assert bulk["n"] >= 1 and bulk["action"] == "approve"
    assert get(ws, "/queue", version=vid, role="reviewer")["queue"] == []   # 验收完队列清空
    assert get(ws, "/review_log", version=vid)["log"]


# ---------------------------------------------------------------------- 真服务器

@pytest.fixture(scope="module")
def live(stack, tmp_path_factory):
    """起一次标准库服务器，用 HTTP 往返把整条链跑一遍。"""
    from types import SimpleNamespace

    ws, data, root, did, vid = stack
    static = tmp_path_factory.mktemp("static")
    (static / "index.html").write_text("<!doctype html><title>mdva</title><div>壳</div>",
                                       encoding="utf-8")
    (static / "assets").mkdir()
    (static / "assets" / "index-Ab12cD.js").write_text("console.log(1)", encoding="utf-8")
    (static.parent / "secret.txt").write_text("DO-NOT-READ", encoding="utf-8")

    httpd = make_server(ws, "127.0.0.1", 0, static_dir=static)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(ws=ws, data=data, did=did, vid=vid, static=static, httpd=httpd,
                          port=httpd.server_address[1])
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def http(method, port, path, body=None, headers=None, raw_body=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=30)
    heads = dict(headers or {})
    payload = raw_body
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        heads["Content-Type"] = "application/json"
    conn.request(method, path, payload, heads)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), data


def test_live_health_and_dataset(live):
    status, heads, raw = http("GET", live.port, "/api/health")
    assert status == 200 and json.loads(raw)["ok"] is True
    assert heads["Cache-Control"] == "no-store"                 # 刚标的东西立刻看得见
    status, _, raw = http("GET", live.port, "/api/dataset")     # 多个数据集时必须指名
    assert status == 404 and "不明确" in json.loads(raw)["error"]
    status, _, raw = http("GET", live.port, f"/api/dataset?dataset={quote('航天标注')}")
    assert status == 200 and json.loads(raw)["id"] == live.did   # 名称也能定位


def test_live_frame_is_a_real_png_url(live):
    uri = quote(str(live.data / "image_field.fits"))
    path = f"/api/frame?width=256&height=160&uri={uri}"
    status, heads, raw = http("GET", live.port, path)
    assert status == 200 and raw.startswith(b"\x89PNG")
    assert heads["Content-Type"] == "image/png"
    assert heads["Cache-Control"].startswith("private")          # 图片可以让浏览器缓存
    status, heads, raw = http("HEAD", live.port, path)
    assert status == 200 and raw == b"" and int(heads["Content-Length"]) > 0


def test_live_post_roundtrip_and_body_validation(live):
    status, _, raw = http("POST", live.port, "/api/analyze",
                          {"uri": str(live.data / "image_field.fits"),
                           "analyzers": ["bg_stats"]})
    assert status == 200 and json.loads(raw)["metrics"]
    status, _, raw = http("POST", live.port, "/api/analyze", raw_body=b"",
                          headers={"Content-Type": "application/json"})
    assert status == 400                                      # 缺 uri 是参数问题，不是 500
    assert "uri" in json.loads(raw)["error"]
    status, _, raw = http("POST", live.port, "/api/analyze",
                          raw_body="{不是 JSON".encode("utf-8"),
                          headers={"Content-Type": "application/json"})
    assert status == 400 and "JSON" in raw.decode("utf-8")
    status, _, raw = http("POST", live.port, "/api/analyze",
                          raw_body=b"1" * (MAX_BODY + 8),
                          headers={"Content-Type": "application/json"})
    assert status == 413                                       # 过大与语法错分开报
    assert "过大" in raw.decode("utf-8")


def test_live_serves_frontend_and_refuses_traversal(live):
    status, heads, raw = http("GET", live.port, "/")
    assert status == 200 and "mdva" in raw.decode("utf-8")
    assert heads["Cache-Control"] == "no-store"                 # index 变了立刻重取
    status, heads, raw = http("GET", live.port, "/assets/index-Ab12cD.js")
    assert status == 200 and b"console.log" in raw
    assert "immutable" in heads["Cache-Control"]               # 带哈希的产物长期缓存
    status, _, raw = http("GET", live.port, "/annotate")      # 前端路由，不该 404
    assert status == 200 and "壳" in raw.decode("utf-8")
    status, _, raw = http("GET", live.port, "/../secret.txt")
    assert b"DO-NOT-READ" not in raw                            # 越界一律拿不到内容
    status, _, _ = http("POST", live.port, "/index.html")
    assert status == 405                                        # 静态目录不可写
    status, heads, _ = http("OPTIONS", live.port, "/api/annotations")
    assert status == 204 and "POST" in heads["Access-Control-Allow-Methods"]


def test_live_other_methods_get_405_json(live):
    """DEV-01（契约核对 v1.2）：PUT/DELETE/PATCH 必须回 405 JSON，而不是 stdlib 的 501 HTML。"""
    for method in ("PUT", "DELETE", "PATCH"):
        status, heads, raw = http(method, live.port, "/api/items")
        assert status == 405, method
        assert "json" in heads.get("Content-Type", ""), method
        assert "GET/POST" in json.loads(raw)["error"], method


def test_live_stays_consistent_under_scrubbing(live):
    """拖动时间轴/框选就是几十个并发请求：帧缓存与 sqlite 不能在其中出错。"""
    import concurrent.futures

    uri = quote(str(live.data / "cube_sim.fits"))

    def fetch(index):
        path = f"/api/frame?uri={uri}&width={160 + (index % 6)}&height=120&index={index}"
        return http("GET", live.port, path)[0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(fetch, range(24)))
    assert codes == [200] * 24
    assert call(live.ws, "GET", "/api/annotations", {"dataset": live.did}).status == 200


def test_refuses_to_bind_a_public_address(stack):
    ws = stack[0]
    with pytest.raises(ValueError):
        make_server(ws, "0.0.0.0", 0)
    assert is_loopback("127.0.0.1") and is_loopback("localhost")
    assert not is_loopback("10.0.0.5")


def test_static_dir_is_package_relative_not_cwd_relative():
    """默认产物目录跟着包走：装成 wheel 或从别的工作目录启动都要能找到前端。"""
    import mdva.web.server as srv

    assert STATIC_DIR == Path(srv.__file__).resolve().parent / "static"


def test_missing_frontend_tells_the_user_how_to_build(tmp_path):
    """前端还没构建时，打开首页要看到命令，而不是一片空白或 404。"""
    ws = Workspaces(tmp_path)
    httpd = make_server(ws, "127.0.0.1", 0, static_dir=tmp_path / "还没有这个目录")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]
        status, _, text_raw = http("GET", port, "/")
        text = text_raw.decode("utf-8")
        assert status == 200 and "npm run build" in text
        assert "前端还没构建" in text and "/api" in text
        assert call(ws, "GET", "/api/health").status == 200          # 接口照常可用
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        ws.close()
