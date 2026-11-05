"""数据整理（复制 / 移动 / 撤销）的往返测试。

这一层是唯一会动到磁盘原始文件的模块，所以每个用例都要证明：
原件没坏、撤销能精确还原、对不上清单的文件绝不删。
"""

import json
from pathlib import Path

import pytest


@pytest.fixture
def tree(tmp_path):
    """两个学科目录 + 两个同名不同目录的掩膜，模拟真实的分割输出布局。"""
    a = tmp_path / "aerospace" / "telemetry"
    b = tmp_path / "cv" / "images"
    c = tmp_path / "medical" / "images"
    for d in (a, b, c):
        d.mkdir(parents=True)
    (a / "eng.csv").write_text("utc,v\n1000,1.0\n1001,2.0\n", encoding="utf-8")
    (a / "ops.json").write_text('{"events": []}', encoding="utf-8")
    (b / "scene.png").write_bytes(b"AAA")
    (c / "scene.png").write_bytes(b"BBB")
    out = tmp_path / "picked"
    return tmp_path, [a / "eng.csv", a / "ops.json", b / "scene.png", c / "scene.png"], out


def test_copy_keeps_originals_and_writes_manifest(tree):
    from mdva import collect

    _, srcs, out = tree
    coll = collect.collect(srcs, out, mode="copy")
    assert coll.summary()["copied"] == 4
    assert all(s.exists() for s in srcs)
    assert coll.manifest_path.is_file()
    # 按"来源目录名/文件名"落位，两个 scene.png 都活着
    assert (out / "telemetry" / "eng.csv").read_bytes() == srcs[0].read_bytes()
    landed = list((out / "images").glob("scene*.png"))
    assert len(landed) == 2
    assert {p.read_bytes() for p in landed} == {b"AAA", b"BBB"}
    assert any(e.renamed for e in coll.entries)


def test_undo_copy_removes_only_the_copies(tree):
    from mdva import collect

    _, srcs, out = tree
    coll = collect.collect(srcs, out, mode="copy")
    report = collect.undo(coll)
    assert report["removed"] == 4 and report["refused"] == 0
    assert not (out / "telemetry" / "eng.csv").exists()
    assert collect.latest_manifest(out) is None
    assert all(s.exists() for s in srcs)


def test_move_and_undo_restores_bytes_in_place(tmp_path):
    from mdva import collect

    src = tmp_path / "run" / "img.png"
    src.parent.mkdir()
    src.write_bytes(b"original-bytes")
    out = tmp_path / "picked"

    coll = collect.collect([src], out, mode="move")
    assert not src.exists()
    assert (out / "run" / "img.png").read_bytes() == b"original-bytes"

    report = collect.undo(coll)
    assert report == {"removed": 0, "restored": 1, "refused": 0,
                      "target": str(out), "mode": "move"}
    assert src.read_bytes() == b"original-bytes"
    assert not (out / "run" / "img.png").exists()


def test_undo_refuses_to_delete_a_file_that_no_longer_matches(tree):
    from mdva import collect

    _, srcs, out = tree
    coll = collect.collect(srcs, out, mode="copy")
    landed = out / "telemetry" / "eng.csv"
    landed.write_text("有人改过了\n", encoding="utf-8")

    report = collect.undo(coll)
    assert report["refused"] == 1
    assert landed.read_text(encoding="utf-8") == "有人改过了\n"
    assert report["removed"] == 3


def test_force_undo_still_handles_a_modified_copy(tree):
    from mdva import collect

    _, srcs, out = tree
    coll = collect.collect(srcs, out, mode="copy")
    landed = out / "telemetry" / "eng.csv"
    landed.write_text("有人改过了\n", encoding="utf-8")
    report = collect.undo(coll, force=True)
    assert report["removed"] == 4 and report["refused"] == 0


def test_undo_removes_empty_directories_only_when_asked(tree, tmp_path):
    from mdva import collect

    _, srcs, out = tree
    collect.undo(collect.collect(srcs, out, mode="copy"), keep_dirs=True)
    assert (out / "telemetry").is_dir()

    out2 = tmp_path / "picked2"
    collect.undo(collect.collect(srcs, out2, mode="copy"), keep_dirs=False)
    assert not (out2 / "telemetry").exists()


def test_flatten_still_avoids_overwriting(tree):
    from mdva import collect

    _, srcs, out = tree
    pairs = collect.plan(srcs, out, flatten=True)
    dsts = [d for _s, d in pairs]
    assert len(set(dsts)) == len(dsts)
    coll = collect.collect(srcs, out, mode="copy", flatten=True)
    assert len(list(out.glob("scene*.png"))) == 2
    assert coll.summary()["copied"] == 4


def test_frame_directory_is_copied_as_a_tree(tmp_path):
    from mdva import collect

    frames = tmp_path / "aerospace" / "sequence" / "solar_evolution"
    frames.mkdir(parents=True)
    for i in range(3):
        (frames / f"f{i:03d}.png").write_bytes(b"x" * (i + 1))
    (frames / "meta.json").write_text(json.dumps({"fps": 2.0}), encoding="utf-8")
    out = tmp_path / "picked"

    coll = collect.collect([frames], out, mode="copy")
    landed = next(p for p in out.rglob("solar_evolution") if p.is_dir())
    assert len(list(landed.glob("f*.png"))) == 3

    report = collect.undo(coll)
    assert report["removed"] == 1
    assert frames.is_dir() and len(list(frames.glob("*.png"))) == 3


def test_dataitem_objects_and_plain_paths_are_interchangeable(tmp_path):
    from mdva import collect, io

    f = tmp_path / "series" / "t.csv"
    f.parent.mkdir(parents=True)
    f.write_text("utc,v\n1000,1.0\n1001,2.0\n", encoding="utf-8")
    item = io.load_any(str(f))

    assert collect.uri_of(item) == str(f)
    assert collect.uri_of(f) == str(f)
    coll = collect.collect([item], tmp_path / "picked", mode="copy")
    assert coll.summary()["copied"] == 1


def test_collect_manifest_is_invisible_to_the_scanner(tmp_path):
    """整理清单落在数据目录里，不能被当成一路数据渲染出来。"""
    from mdva import collect, io

    src = tmp_path / "raw"
    src.mkdir()
    (src / "t.csv").write_text("utc,v\n1000,1.0\n1001,2.0\n", encoding="utf-8")
    collect.collect([src / "t.csv"], src / "picked", mode="copy")
    assert (src / "picked" / collect.MANIFEST_NAME).is_file()

    names = [Path(i.uri).name for i in io.scan(str(src))]
    assert collect.MANIFEST_NAME not in names
    assert "t.csv" in names


def test_missing_source_is_skipped_not_fatal(tmp_path):
    from mdva import collect

    coll = collect.collect([tmp_path / "nope.csv"], tmp_path / "out", mode="copy")
    assert coll.entries == [] and len(coll.skipped) == 1
    assert collect.undo(coll)["refused"] == 0


def test_unknown_mode_is_rejected(tmp_path):
    from mdva import collect

    with pytest.raises(ValueError):
        collect.collect([tmp_path / "x"], tmp_path / "out", mode="link")


def test_manifest_round_trips_through_disk(tmp_path):
    from mdva.collect import Collection, collect

    src = tmp_path / "a" / "x.csv"
    src.parent.mkdir()
    src.write_text("utc,v\n1000,1.0\n", encoding="utf-8")
    coll = collect([src], tmp_path / "picked", mode="copy")

    back = Collection.load(coll.manifest_path)
    assert back.mode == "copy"
    assert [e.dst for e in back.entries] == [e.dst for e in coll.entries]
    assert back.summary()["copied"] == 1
