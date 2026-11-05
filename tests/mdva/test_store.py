"""SQLite 存储门面的测试。

重点是三件事：版本 DAG 只增不删（回退=以旧版为父产新版）、每次状态迁移都留痕、
以及标注层的任何操作都不能碰到磁盘上的原始数据文件。
"""

import hashlib
import threading
import time

import pytest

from mdva.annotate import state as ST
from mdva.annotate.model import Ann
from mdva.annotate.store import SCHEMA_VERSION, DATASET_TYPES, Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "mdva.sqlite")
    yield s
    s.close()


@pytest.fixture
def ds(store, tmp_path):
    """一个数据集 + 它的第一个版本。"""
    did = store.create_dataset("耀斑标注", "detection", tmp_path / "data")
    return did, store.put_version(did, None, "init")


def _snap(paths):
    return {str(p): (p.exists(), hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "",
                     p.stat().st_mtime_ns if p.exists() else 0) for p in paths}


# ---------------------------------------------------------------------- 建库与重开

def test_init_is_idempotent_and_stamps_user_version(store, tmp_path):
    assert (tmp_path / "mdva.sqlite").is_file()
    before = store.db.execute("PRAGMA user_version").fetchone()[0]
    store.init()
    store.init()
    assert before == SCHEMA_VERSION == store.db.execute("PRAGMA user_version").fetchone()[0]


def test_reopening_the_same_db_keeps_rows(tmp_path, store, ds):
    did, vid = ds
    store.put_annotation(vid, "cube/a.png", Ann("box", [1, 2, 3, 4], "flare"))
    store.close()
    again = Store(tmp_path / "mdva.sqlite")
    assert again.get_dataset(did)["name"] == "耀斑标注"
    assert len(again.list_annotations(vid)) == 1
    again.close()


def test_unknown_dataset_type_rejected(store):
    with pytest.raises(ValueError):
        store.create_dataset("x", "speech", "root")
    assert "detection" in DATASET_TYPES


def test_dataset_and_version_lookup_errors(store):
    assert store.get_dataset("nope") is None
    with pytest.raises(KeyError):
        store.put_version("nope", None, "init")
    did = store.create_dataset("d", "mixed", "root")
    with pytest.raises(KeyError):
        store.put_version(did, "vmissing", "clean")
    with pytest.raises(KeyError):
        store.put_annotation("vmissing", "u", Ann("box", [0, 0, 1, 1]))


# ---------------------------------------------------------------------- 版本 DAG

def test_rollback_creates_a_child_and_keeps_history(store, ds):
    did, v1 = ds
    v2 = store.put_version(did, v1, "clean", {"op": "dedup"})
    v3 = store.put_version(did, v1, "rollback", {"to": v1})
    assert store.get_version(v2) is not None            # 历史一行都没少
    assert [v["id"] for v in store.lineage(v3)] == [v1, v3]
    assert store.head_version(did) in {v2, v3}
    assert [v["id"] for v in store.list_versions(did)] == [v1, v2, v3]
    assert store.diff(v1, v2) == {"added": [], "removed": [], "n_added": 0, "n_removed": 0,
                                  "n_common": 0}


def test_version_counts_annotations(store, ds):
    did, vid = ds
    child = store.put_version(did, vid, "preannotate")
    for i in range(3):
        store.put_annotation(vid, f"img/{i}.png", Ann("box", [0, 0, 10, 10], "a"))
    store.put_annotation(child, "img/0.png", Ann("box", [0, 0, 5, 5], "a"))
    counts = {v["id"]: v["n_annotations"] for v in store.list_versions(did)}
    assert counts == {vid: 3, child: 1}


def test_annotations_never_touch_source_files(store, ds, tmp_path):
    """这一层只写数据库：原文件必须字节不变、mtime 不变、还在原地。"""
    did, vid = ds
    data = tmp_path / "data" / "images"
    data.mkdir(parents=True)
    files = []
    for i in range(4):
        p = data / f"s{i}.png"
        p.write_bytes(bytes([i]) * 64)
        files.append(p)
    before = _snap(files)

    ids = [store.put_annotation(vid, str(f), Ann("box", [0, 0, 8, 8], "x")) for f in files]
    for aid in ids:
        store.set_state(aid, "annotator", "submit")
    store.review_bulk(vid, "admin", "approve", "批量")
    store.delete_annotation(ids[0], "admin", "误标")
    store.export_manifest(did)
    store.put_version(did, vid, "augment", {"chain": ["flip"]})

    assert _snap(files) == before
    assert all(p.is_file() for p in files)


# ---------------------------------------------------------------------- 标注读写

def test_annotation_roundtrip_keeps_every_field(store, ds):
    did, vid = ds
    ann = Ann("poly", [0, 0, 10, 0, 10, 8], "器官", source="visual_prompt", confidence=0.42,
              backend="template_match", slice=7, level=2, color=(255, 80, 80), note="边界模糊")
    aid = store.put_annotation(vid, "medical/slice.nii", ann)
    got = store.get_annotation(aid)
    assert got["item_uri"] == "medical/slice.nii"
    assert got["state"] == "draft" and got["role"] == ""
    back = got["ann"]
    assert (back.geom, back.label, back.source) == ("poly", "器官", "visual_prompt")
    assert back.coords == ann.coords and back.confidence == pytest.approx(0.42)
    assert (back.backend, back.slice, back.level, back.color, back.note) == (
        "template_match", 7, 2, (255, 80, 80), "边界模糊")


def test_upsert_with_explicit_id_does_not_duplicate(store, ds):
    did, vid = ds
    ann = Ann("box", [0, 0, 10, 10], "点源")
    aid = store.put_annotation(vid, "a.png", ann)
    again = store.put_annotation(vid, "a.png", Ann("box", [1, 1, 11, 11], "点源"),
                                 annotation_id=aid)
    assert again == aid
    rows = store.list_annotations(vid)
    assert len(rows) == 1 and rows[0]["ann"].coords == [1.0, 1.0, 11.0, 11.0]


def test_list_annotations_filters(store, ds):
    did, vid = ds
    store.put_annotation(vid, "x/1.png", Ann("box", [0, 0, 1, 1], "a"))
    store.put_annotation(vid, "x/1.png", Ann("point", [4, 4], "a"), state="submitted")
    store.put_annotation(vid, "x/2.png",
                         Ann("box", [0, 0, 1, 1], "b", source="preannotate", confidence=0.3))
    assert len(store.list_annotations(vid)) == 3
    assert len(store.list_annotations(vid, item_uri="x/1.png")) == 2
    assert [d["ann"].geom for d in store.list_annotations(vid, state="submitted")] == ["point"]
    assert [d["item_uri"] for d in store.list_annotations(vid, source="preannotate")] == ["x/2.png"]
    assert store.annotated_items(vid) == {"x/1.png", "x/2.png"}
    assert store.get_annotation("a11111111111111") is None


def test_bad_state_and_geom_rejected_on_write(store, ds):
    did, vid = ds
    with pytest.raises(ValueError):
        store.put_annotation(vid, "a", Ann("box", [0, 0, 1, 1]), state="merged")
    with pytest.raises(ValueError):
        store.put_annotation(vid, "a", Ann("interval", [5, 1]))


def test_progress_and_label_counts(store, ds):
    did, vid = ds
    ids = [store.put_annotation(vid, f"i/{n}.png", Ann("box", [0, 0, 1, 1], n))
           for n in ("flare", "flare", "artifact")]
    store.set_state(ids[0], "annotator", "submit")
    store.set_state(ids[0], "reviewer", "approve")
    p = store.progress(vid)
    assert p["total"] == 3 and p["approved"] == 1 and p["ratio"] == pytest.approx(0.3333, abs=1e-3)
    assert p["by_state"] == {"approved": 1, "draft": 2}
    assert store.label_counts(vid) == {"flare": 2, "artifact": 1}


# ---------------------------------------------------------------------- 标签组

def test_labels_are_replaced_wholesale_and_ordered(store, ds):
    did, vid = ds
    store.set_labels(did, [{"name": "耀斑", "color": "#ff5050", "hotkey": "1"},
                           {"name": "点源", "color": "#50c8ff", "hotkey": "2"}])
    assert [l["name"] for l in store.list_labels(did)] == ["耀斑", "点源"]
    store.set_labels(did, [{"name": "伪影"}])
    assert [l["name"] for l in store.list_labels(did)] == ["伪影"]
    assert store.list_labels(did)[0]["order"] == 0
    with pytest.raises(ValueError):
        store.set_labels(did, [{"name": "a"}, {"name": "a"}])


# ---------------------------------------------------------------------- 状态机留痕

def test_full_review_flow_writes_a_trail(store, ds):
    did, vid = ds
    aid = store.put_annotation(vid, "t/1.csv", Ann("interval", [0.0, 1.5], "异常段"), role="")
    assert store.set_state(aid, "annotator", "submit") == "submitted"
    assert store.set_state(aid, "reviewer", "reject", "起点偏了 0.2s") == "rejected"
    assert store.set_state(aid, "annotator", "submit") == "submitted"
    assert store.set_state(aid, "reviewer", "approve") == "approved"
    log = store.review_log(aid)
    assert [(r["role"], r["action"], r["from"], r["to"]) for r in log] == [
        ("annotator", "submit", "draft", "submitted"),
        ("reviewer", "reject", "submitted", "rejected"),
        ("annotator", "submit", "rejected", "submitted"),
        ("reviewer", "approve", "submitted", "approved")]
    assert log[1]["reason"] == "起点偏了 0.2s"
    assert all(r["version_id"] == vid and r["dataset_id"] == did for r in log)
    assert {r["item_uri"] for r in store.review_log(version_id=vid)} == {"t/1.csv"}


def test_reviewer_cannot_submit_and_annotator_cannot_approve(store, ds):
    did, vid = ds
    aid = store.put_annotation(vid, "u", Ann("box", [0, 0, 1, 1]))
    with pytest.raises(ST.PermissionDenied):
        store.set_state(aid, "reviewer", "submit")
    with pytest.raises(ST.PermissionDenied):
        store.set_state(aid, "annotator", "approve")
    assert store.get_annotation(aid)["state"] == "draft"   # 越权不留痕也不改状态
    with pytest.raises(KeyError):
        store.set_state("a11111111111111", "admin", "approve")   # 不存在的标注要报错


def test_admin_can_reopen_and_state_of_unknown_is_clear(store, ds):
    did, vid = ds
    aid = store.put_annotation(vid, "u", Ann("box", [0, 0, 1, 1]))
    store.set_state(aid, "annotator", "submit")
    store.set_state(aid, "reviewer", "approve")
    assert store.set_state(aid, "admin", "reopen", "复审") == "draft"
    with pytest.raises(ST.PermissionDenied):
        store.set_state(aid, "admin", "reopen")            # draft 无需重新打开


def test_submit_many_and_review_bulk(store, ds):
    did, vid = ds
    for i in range(3):
        store.put_annotation(
            vid, "same.png",
            Ann("box", [i, i, i + 5, i + 5], "x", source="preannotate",
                confidence=0.5 + 0.2 * i, backend="template_match"))
    store.put_annotation(vid, "other.png", Ann("box", [0, 0, 1, 1], "x"))
    assert store.submit_many(vid, "same.png", "annotator") == 3
    assert store.submit_many(vid, "same.png", "annotator") == 0
    assert store.review_bulk(vid, "reviewer", "approve", "高置信自动验收",
                             max_confidence=0.6) == 2
    states = sorted(d["state"] for d in store.list_annotations(vid))
    assert states == ["approved", "approved", "draft", "submitted"]
    assert store.queue(vid, "reviewer") == ["same.png"]      # 低置信那条还等着人看


def test_queue_is_per_role(store, ds):
    did, vid = ds
    store.put_annotation(vid, "q/draft.png", Ann("box", [0, 0, 1, 1]))
    b = store.put_annotation(vid, "q/to_review.png", Ann("box", [0, 0, 1, 1]))
    store.set_state(b, "annotator", "submit")
    assert store.queue(vid, "annotator") == ["q/draft.png"]
    assert store.queue(vid, "reviewer") == ["q/to_review.png"]
    assert set(store.queue(vid, "admin")) == {"q/draft.png", "q/to_review.png"}
    c = store.put_annotation(vid, "q/rejected.png", Ann("box", [0, 0, 1, 1]))
    store.set_state(b, "reviewer", "reject")
    store.set_state(c, "admin", "reject")
    # 驳回的活儿回到标注员队列，审核员那边清空
    assert store.queue(vid, "annotator") == ["q/draft.png", "q/rejected.png", "q/to_review.png"]
    assert store.queue(vid, "reviewer") == []
    assert ST.queue_states("reviewer") == ("submitted",)


def test_delete_annotation_logs_and_is_honest_about_missing(store, ds):
    did, vid = ds
    aid = store.put_annotation(vid, "d.png", Ann("box", [0, 0, 1, 1], "y"))
    assert store.delete_annotation(aid, "admin", "重复框") is True
    assert store.get_annotation(aid) is None
    assert store.delete_annotation(aid) is False
    assert store.list_annotations(vid) == []
    log = store.review_log(aid)
    assert [(r["role"], r["action"], r["to"], r["reason"]) for r in log] == [
        ("admin", "delete", "deleted", "重复框")]


# ---------------------------------------------------------------------- 版本比对

def test_diff_reports_added_and_removed(store, ds):
    did, vid = ds
    child = store.put_version(did, vid, "preannotate", {"backend": "template_match"})
    store.put_annotation(vid, "img/1.png", Ann("box", [0, 0, 10, 10], "flare"))
    store.put_annotation(vid, "img/2.png", Ann("point", [3, 4], "star"))
    store.put_annotation(child, "img/1.png", Ann("box", [0, 0, 10, 10], "flare"))
    store.put_annotation(child, "img/3.png", Ann("box", [1, 1, 4, 4], "flare"))
    d = store.diff(vid, child)
    assert d["n_common"] == 1 and d["n_added"] == 1 and d["n_removed"] == 1
    assert any("img/3.png" in s for s in d["added"])
    assert any("img/2.png" in s for s in d["removed"])


# ---------------------------------------------------------------------- 任务表

def test_task_lifecycle_keeps_version_out_when_not_given(store, ds):
    did, vid = ds
    out = store.put_version(did, vid, "clean")
    tid = store.create_task("clean", did, vid, out, {"ops": ["dedup"]})
    assert store.get_task(tid)["state"] == "running"
    assert store.get_task(tid)["params"] == {"ops": ["dedup"]}
    store.update_task(tid, "done", {"removed": 12})
    assert store.get_task(tid)["version_out"] == out
    store.update_task(tid, "failed", {"error": "boom"})
    t = store.get_task(tid)
    assert t["version_out"] == out and t["report"]["error"] == "boom"
    assert store.get_task("tmissing") is None
    assert [x["kind"] for x in store.list_tasks(did, "clean")] == ["clean"]
    assert store.list_tasks(did, "augment") == []
    assert store.list_tasks()[0]["id"] == tid


# ---------------------------------------------------------------------- manifest

def test_manifest_is_derived_and_topologically_ordered(store, ds, tmp_path):
    did, v1 = ds
    v2 = store.put_version(did, v1, "clean")
    v3 = store.put_version(did, v2, "augment")
    store.put_annotation(v3, "img/1.png", Ann("box", [0, 0, 1, 1], "flare"))
    store.set_labels(did, [{"name": "flare", "hotkey": "1"}])
    tid = store.create_task("export", did, v3, None, {"format": "coco"})
    store.update_task(tid, "done", {"path": "out/coco.json", "images": 1})
    m = store.export_manifest(did)
    assert [v["id"] for v in m["versions"]] == [v1, v2, v3]
    assert m["versions"][0]["parent"] is None and m["versions"][-1]["n_annotations"] == 1
    assert m["dataset"] == "耀斑标注" and m["type"] == "detection"
    assert m["labels"][0]["name"] == "flare" and m["schema_version"] == SCHEMA_VERSION
    assert m["exports"] == [{"path": "out/coco.json", "images": 1}]
    text = (tmp_path / "data" / "manifest.json").read_text(encoding="utf-8")
    assert "耀斑标注" in text and v2 in text
    assert store.export_manifest(did, write=False)["versions"][0]["id"] == v1
    with pytest.raises(KeyError):
        store.export_manifest("dsmissing")


def test_manifest_keeps_orphan_versions(store):
    """跨数据集当父版本在 DAG 里是合法的，导出时不能把它丢掉。"""
    a = store.create_dataset("A", "mixed", "root_a")
    b = store.create_dataset("B", "mixed", "root_b")
    va = store.put_version(a, None, "init")
    vb = store.put_version(b, va, "derived")      # vb 的父在数据集 b 的列表之外
    m = store.export_manifest(b, write=False)
    assert [v["id"] for v in m["versions"]] == [vb]
    assert m["versions"][0]["parent"] == va
    assert [v["id"] for v in store.lineage(vb)] == [va, vb]


# ---------------------------------------------------------------------- 并发

def test_store_survives_worker_threads(tmp_path):
    """FastAPI 会在线程池里跑同步 handler，写锁必须挡住交叉写。"""
    s = Store(tmp_path / "mdva.sqlite")
    did = s.create_dataset("并发", "mixed", tmp_path / "d")
    vid = s.put_version(did, None, "init")
    errors = []

    def worker(n):
        try:
            for i in range(20):
                s.put_annotation(vid, f"t{n}/{i}.png", Ann("box", [0, 0, 1, 1], f"l{i}"))
        except Exception as exc:      # noqa: BLE001 - 测试里要把线程异常带回主线程
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    t0 = time.time()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []
    assert len(s.list_annotations(vid)) == 120
    assert time.time() - t0 < 30
    s.close()
