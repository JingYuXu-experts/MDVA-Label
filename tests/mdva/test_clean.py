"""清洗层：判定、预览=实跑、原件不动、一键回退、标注继承。

这一层的验收点只有四条，缺一条它就是假的：
1. `preview()` 报的剔除数和 `apply()` 实际剔除的是同一批（共用 `evaluate()`）；
2. 任何清洗都不许碰原始文件（用 size + mtime_ns 证明）；
3. 一键回退只删我们自己写出去的文件，且删前要核对字节数；
4. 清洗完的新版本必须还带着原来的标注——否则导出会得到空数据集。
"""

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from scipy import ndimage

from mdva import clean, io
from mdva.annotate.model import Ann
from mdva.annotate.store import Store
from mdva.backends.base import Prompt
from mdva.io.series import Calibration


def blobs(size=160, positions=((22, 26), (84, 100), (132, 44)), amp=420.0, seed=0):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    arr = rng.normal(30.0, 2.0, (size, size))
    for cx, cy in positions:
        arr += amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 6.0 ** 2))
    arr = (arr - arr.min()) / (arr.max() - arr.min()) * 255.0
    return arr.astype(np.uint8)


def save(path, arr) -> str:
    Image.fromarray(np.ascontiguousarray(np.asarray(arr))).save(str(path))
    return str(path)


def name(uri) -> str:
    return Path(str(uri)).name


def stat(path) -> tuple:
    st = Path(str(path)).stat()
    return (st.st_size, st.st_mtime_ns)


@pytest.fixture
def images(tmp_path):
    """两张完全相同的 + 一张内容不同的 + 一张纯色（必然判为模糊）。"""
    sharp = blobs()
    return {
        "a": save(tmp_path / "a.png", sharp),
        "b": save(tmp_path / "b.png", sharp),
        "c": save(tmp_path / "c.png", blobs(seed=7, positions=((60, 60),))),
        "blur": save(tmp_path / "blur.png", np.full((160, 160), 128, dtype=np.uint8)),
    }


@pytest.fixture
def items(images):
    return [io.load_any(images[k]) for k in ("a", "b", "c", "blur")]


@pytest.fixture
def store(tmp_path, images):
    s = Store(tmp_path / "mdva.sqlite")
    did = s.create_dataset("清洗试验", "detection", tmp_path)
    vid = s.put_version(did, None, "init")
    s.set_labels(did, [{"name": "blob"}])
    for key in ("a", "b", "c"):
        s.put_annotation(vid, images[key], Ann("box", [12.0, 16.0, 40.0, 44.0], "blob"),
                         role="annotator", state="submitted")
    yield s, did, vid, tmp_path
    s.close()


# ---------------------------------------------------------------------- 注册与参数

def test_registry_lists_every_op():
    assert set(clean.CLEAN_OPS.names()) == {"dedup", "blur", "content_filter",
                                            "series_clean", "crop", "rotate", "mirror"}


def test_build_rejects_unknown_and_requires_name():
    with pytest.raises(KeyError):
        clean.build([{"name": "nope"}])
    with pytest.raises(ValueError):
        clean.build([{"params": {"threshold": 1}}])


def test_build_folds_top_level_keys_into_params():
    assert clean.build([{"name": "blur", "threshold": 12.0}])[0].params["threshold"] == 12.0


def test_build_accepts_prompt_dicts():
    ops = clean.build([{"name": "content_filter", "params": {
        "prompts": [Prompt("box", [1, 1, 9, 9], "x")]}}])
    assert isinstance(ops[0].prompts[0], Prompt)


# ---------------------------------------------------------------------- 去相似 / 去模糊

def test_dedup_groups_only_true_duplicates(items):
    groups = clean.Dedup().groups(items)
    assert len(groups) == 1 and sorted(name(u) for u in groups[0]) == ["a.png", "b.png"]


def test_dedup_two_stage_is_what_separates_different_content(items):
    """把第一关放到最松（汉明距离 64），第二关的布局相关性才是真正把关的那一步。"""
    loose = {name(d.uri) for d in clean.Dedup(distance=64, content_corr=-1.0)
             .evaluate(items) if not d.keep}
    guarded = {name(d.uri) for d in clean.Dedup(distance=64).evaluate(items) if not d.keep}
    assert len(loose) == 2, "只按哈希会把内容不同的 a/c 并进来"
    assert guarded < loose, "布局相关性必须把 a/b 之外分开"


def test_dedup_keep_first_and_last_differ(items):
    first = {name(d.uri) for d in clean.Dedup(keep="first").evaluate(items) if not d.keep}
    last = {name(d.uri) for d in clean.Dedup(keep="last").evaluate(items) if not d.keep}
    assert first != last and len(first) == len(last) == 1


def test_dedup_keep_sharpest_uses_blur_scores(tmp_path):
    from mdva.clean.ops import Blur

    sharp = blobs()
    soft = ndimage.gaussian_filter(sharp.astype(np.float64), 1.0).astype(np.uint8)
    hi = io.load_any(save(tmp_path / "hi.png", sharp))
    lo = io.load_any(save(tmp_path / "soft.png", soft))
    groups = clean.Dedup().groups([hi, lo])
    assert len(groups) == 1, "轻度模糊的同内容图必须先进同一重复组，否则没得挑"

    scores = {str(i.uri): Blur().sharpness(i) for i in (hi, lo)}
    assert scores[str(hi.uri)] > scores[str(lo.uri)]
    kept = [name(d.uri) for d in clean.Dedup(keep="sharpest").evaluate([hi, lo], scores)
            if d.keep]
    assert kept == ["hi.png"], "同内容两版里必须留清晰的那版"


def test_blur_sharpness_is_unit_independent(tmp_path):
    """同一张图存成 FITS（计数）和 PNG（0-255）必须给同一个清晰度分。

    不归一的话阈值只能对某一种位深"看着对"，换个数据源就要重调——那没人用得起来。
    """
    from mdva import synth
    from mdva.clean.ops import Blur

    sharp = blobs()
    png = io.load_any(save(tmp_path / "u8.png", sharp))
    fits = io.load_any(str(synth.write_fits(tmp_path / "counts.fits",
                                           sharp.astype(np.float64) * 50.0 + 1000.0,
                                           {"INSTRUME": "TEST"})))
    a, b = Blur().sharpness(png), Blur().sharpness(fits)
    assert abs(a - b) / max(a, b) < 0.05, f"PNG {a:.1f} vs FITS {b:.1f}"


def test_blur_drops_flat_images(items):
    dropped = {name(d.uri) for d in clean.Blur(threshold=40.0).evaluate(items) if not d.keep}
    assert dropped == {"blur.png"}, {name(d.uri): round(d.score, 2) for d in
                                     clean.Blur(threshold=40.0).evaluate(items)}


def test_blur_percentile_overrides_fixed_threshold(items):
    decisions = clean.Blur(threshold=0.0, percentile=50.0).evaluate(items)
    cut = float(np.percentile([d.score for d in decisions], 50))
    dropped = {name(d.uri) for d in decisions if not d.keep}
    assert dropped == {name(d.uri) for d in decisions if d.score < cut}
    assert "blur.png" in dropped
    # 同一个 threshold=0 不分位时什么都不该剔——分位数模式才是按这批数据自己切的
    assert all(d.keep for d in clean.Blur(threshold=0.0).evaluate(items))


# ---------------------------------------------------------------------- 时序清洗

def _telemetry_fixture(tmp_path):
    t = np.arange(600, dtype=np.float64)
    base = np.column_stack([np.sin(t / 50.0) * 10.0 + 20.0, (t // 100).astype(float)])
    calib = {"SA_Temp": {"type": "analog", "scale": 1.0, "offset": 0.0, "unit": "V"},
             "SA_Mode": {"type": "discrete", "values": {str(k): f"S{k}" for k in range(6)}}}

    def write(tag, values):
        path = tmp_path / f"tel_{tag}.csv"
        rows = ["utc,SA_Temp,SA_Mode"]
        rows += [f"{t[i]:.3f},{values[i, 0]:.6f},{values[i, 1]:.6f}" for i in range(t.size)]
        path.write_text("\n".join(rows), encoding="utf-8")
        side = tmp_path / (path.stem + ".calib.json")
        if not side.is_file():
            side.write_text(json.dumps(calib), encoding="utf-8")
        return io.load_any(str(path))

    return base, calib, write


def test_series_clean_bad_ratio_frozen_and_illegal_code(tmp_path):
    base, calib, write = _telemetry_fixture(tmp_path)
    nan = base.copy()
    nan[:60, 0] = np.nan
    frozen = base.copy()
    frozen[100:400, 0] = 12.34
    illegal = base.copy()
    illegal[50, 1] = 5.0                       # 5 在表里，合法
    illegal[60, 1] = 7.0                       # 7 没登记

    verdicts = {name(d.uri): d for d in clean.SeriesClean(calibration=Calibration(calib)).evaluate(
        [write("ok", base), write("nan", nan), write("frozen", frozen),
         write("illegal", illegal)])}
    assert verdicts["tel_ok.csv"].keep
    assert not verdicts["tel_nan.csv"].keep and "坏点率" in verdicts["tel_nan.csv"].reason
    assert not verdicts["tel_frozen.csv"].keep and "冻结段" in verdicts["tel_frozen.csv"].reason
    assert not verdicts["tel_illegal.csv"].keep and "离散通道" in verdicts["tel_illegal.csv"].reason


def test_series_clean_without_calibration_still_rejects_fractions(tmp_path):
    """没有标定表时，离散通道出现小数说明中间被插过值——那是造出来的工况。"""
    base, _calib, write = _telemetry_fixture(tmp_path)
    half = base.copy()
    half[10, 1] = 1.5
    verdict = clean.SeriesClean().evaluate([write("half", half)])[0]
    assert not verdict.keep and "离散通道" in verdict.reason


def test_evaluate_routes_by_modality(items, tmp_path):
    """图像算子不碰时序、时序算子不碰图像：per_op 的 n 就是路由证据。"""
    base, _calib, write = _telemetry_fixture(tmp_path)
    series = write("ok", base)
    result = clean.evaluate([*items, series], [{"name": "blur", "threshold": 40.0},
                                              {"name": "series_clean"}])
    assert result.per_op["blur"]["n"] == len(items)
    assert result.per_op["series_clean"]["n"] == 1


# ---------------------------------------------------------------------- 内容过滤

def test_content_filter_pins_template_to_one_reference(images, items):
    """提示不带模板时必须钉在同一张参考图上，否则每张图和自己匹配、分数恒 1.0。"""
    prompt = Prompt("box", [12, 16, 32, 36], "blob", source_uri=images["a"])
    anchored = clean.ContentFilter(prompts=[prompt]).anchored(items)
    assert anchored[0].template is not None
    assert name(anchored[0].source_uri) == "a.png"


def test_content_filter_rules_are_opposites(images, items):
    prompt = Prompt("box", [12, 16, 32, 36], "blob", source_uri=images["a"])
    kwargs = {"prompts": [prompt], "min_score": 0.5, "threshold": 0.4}
    keep = {name(d.uri) for d in clean.ContentFilter(rule="keep_if_matched", **kwargs)
            .evaluate(items) if d.keep}
    drop = {name(d.uri) for d in clean.ContentFilter(rule="drop_if_matched", **kwargs)
            .evaluate(items) if d.keep}
    assert "a.png" in keep and "a.png" not in drop
    assert keep != drop


def test_content_filter_rejects_unknown_rule(items, images):
    prompt = Prompt("box", [12, 16, 32, 36], "blob", source_uri=images["a"])
    with pytest.raises(ValueError):
        clean.ContentFilter(prompts=[prompt], rule="keep_if_absent").evaluate(items)


# ---------------------------------------------------------------------- 预览 = 实跑

def test_preview_matches_apply(store, images):
    s, did, vid, root = store
    specs = [{"name": "dedup"}]
    uris = [images["a"], images["b"], images["c"]]
    preview = clean.preview(clean.load_items(uris), specs)
    report = clean.apply(s, vid, specs, uris=uris, out_dir=root / "out")
    assert preview["n_dropped"] == report["summary"]["n_dropped"] == 1
    assert preview["n_kept"] == report["summary"]["n_kept"] == 2
    # M4-A：预览阶段就能拿到逐条命中清单（与 apply 报告同一条代码路径、同一形状）
    assert {d["uri"] for d in preview["dropped"]} == {d["uri"] for d in report["dropped"]}
    assert set(preview["dropped"][0]) == {"uri", "op", "reason", "score"}


def test_apply_never_touches_source_files(store, images):
    s, did, vid, root = store
    before = {k: stat(v) for k, v in images.items()}
    clean.apply(s, vid, [{"name": "blur", "threshold": 1e9}], out_dir=root / "out")
    clean.apply(s, vid, [{"name": "mirror"}], out_dir=root / "out", write_transformed=True)
    assert {k: stat(v) for k, v in images.items()} == before


def test_dropped_items_are_recorded_in_the_version(store, images):
    s, did, vid, root = store
    report = clean.apply(s, vid, [{"name": "dedup"}],
                         uris=[images["a"], images["b"], images["c"]], out_dir=root / "out")
    dropped = clean.dropped_of(s, report["version"])
    assert len(dropped) == 1 and dropped[0]["op"] == "dedup"
    assert len(s.list_annotations(vid)) == 3          # 父版本一条标注都没少


def test_clean_keeps_annotations_of_items_outside_the_subset(store, images):
    """只清洗 a、b 时，c 的标注必须原样进新版本——清洗不许顺手删没参与判定的标签。"""
    s, did, vid, root = store
    report = clean.apply(s, vid, [{"name": "dedup"}], uris=[images["a"], images["b"]],
                         out_dir=root / "out")
    dropped = {d["uri"] for d in report["dropped"]}
    # D3：库内 item_uri 是相对工作区根的相对路径，比对磁盘绝对路径前先还原
    assert {s.to_abs(r["item_uri"]) for r in s.list_annotations(report["version"])} == \
        {images["a"], images["b"], images["c"]} - dropped


def test_inherited_annotations_keep_their_review_state(store, images):
    """清洗不许把已提交的标注打回草稿——审核流程一旦重置，之前的工作量就没了。"""
    s, did, vid, root = store
    report = clean.apply(s, vid, [{"name": "dedup"}],
                         uris=[images["a"], images["b"], images["c"]], out_dir=root / "out")
    rows = s.list_annotations(report["version"])
    assert rows and {r["state"] for r in rows} == {"submitted"}
    assert {r["role"] for r in rows} == {"annotator"}


def test_apply_writes_ops_author_and_task_report(store, images):
    s, did, vid, root = store
    report = clean.apply(s, vid, [{"name": "blur", "params": {"threshold": 5.0}},
                                  {"name": "dedup"}], out_dir=root / "out", author="xjy")
    version = s.get_version(report["version"])
    assert version["op"] == "clean" and version["parent_id"] == vid
    assert [(o["name"], o["params"]) for o in version["params"]["ops"]] == \
        [("blur", {"threshold": 5.0}), ("dedup", {})]
    assert version["params"]["author"] == "xjy"

    task = s.get_task(report["task_id"])
    assert (task["kind"], task["state"]) == ("clean", "done")
    assert task["report"]["summary"]["n_dropped"] == report["summary"]["n_dropped"]
    assert "decisions" not in task["report"]          # 逐条判定只回给调用方，不进报告


# ---------------------------------------------------------------------- 回退

def test_rollback_removes_our_files_and_restores_labels(store, images):
    s, did, vid, root = store
    report = clean.apply(s, vid, [{"name": "crop", "params": {"fraction": 0.5}}],
                         uris=[images["c"]], out_dir=root / "out", write_transformed=True)
    written = [w["written"] for w in report["written"]]
    assert written and all(Path(p).is_file() for p in written)
    assert name(written[0]) == "0000_c.png"

    back = clean.rollback(s, report["version"])
    assert back["removed_files"] == written
    assert not any(Path(p).exists() for p in written)
    assert all(Path(v).is_file() for v in images.values())      # 原件一张不少
    assert back["restored"] == vid and back["new_version"]
    assert len(s.list_annotations(back["new_version"])) == 3    # 回到父版本的标注


def test_rollback_skips_files_someone_else_changed(store, images):
    s, did, vid, root = store
    report = clean.apply(s, vid, [{"name": "mirror"}], uris=[images["c"]],
                         out_dir=root / "out", write_transformed=True)
    target = report["written"][0]["written"]
    Path(target).write_bytes(b"changed by somebody else")
    back = clean.rollback(s, report["version"])
    assert back["removed_files"] == []
    assert back["skipped_files"][0]["why"].startswith("size differs")
    assert Path(target).is_file()                               # 不是我们写的就绝不删


def test_rollback_requires_a_producing_task(store):
    s, did, vid, root = store
    with pytest.raises(ValueError):
        clean.rollback(s, vid)


def test_manifest_lists_every_written_file(store, images):
    s, did, vid, root = store
    report = clean.apply(s, vid, [{"name": "rotate", "params": {"k": 2}}],
                         uris=[images["a"], images["c"]], out_dir=root / "out",
                         write_transformed=True)
    manifest = json.loads((root / "out" / "clean" / report["version"] /
                           "written.json").read_text(encoding="utf-8"))
    assert len(manifest["files"]) == 2
    assert all(f["written"] != f["source"] for f in manifest["files"])
    assert all(f["chain"][0]["op"] == "rotate" for f in manifest["files"])


def test_chain_composes_once_per_item(store, images):
    """裁剪→镜像一条链只写一个文件，不是每步一个。"""
    s, did, vid, root = store
    report = clean.apply(s, vid, [{"name": "crop", "params": {"fraction": 0.6}},
                                  {"name": "mirror"}], uris=[images["c"]],
                         out_dir=root / "out", write_transformed=True)
    assert len(report["written"]) == 1
    rec = report["written"][0]
    assert [c["op"] for c in rec["chain"]] == ["crop", "mirror"]
    assert rec["source_shape"] == [160, 160]
    arr = io.load_any(rec["written"]).load()
    assert arr.shape == (96, 96)          # 0.6 裁剪 160 -> 96，再镜像不改尺寸


# ---------------------------------------------------------------------- 几何映射

@pytest.mark.parametrize("chain,shape", [
    ([("mirror", {"horizontal": True})], (40, 60)),
    ([("rotate", {"k": 1})], (40, 60)),
    ([("crop", {"box": [10, 10, 30, 30]})], (40, 60)),
    ([("mirror", {"horizontal": True}), ("rotate", {"k": 1}),
      ("crop", {"box": [0, 20, 40, 50]})], (40, 60)),
])
def test_map_geometry_lands_where_the_pixels_went(chain, shape):
    """标注几何必须和数组变换落到同一处：拿掩膜当尺子量，不许差一个像素。

    像素按"格子"算：box 的右下是排他的（COCO 口径），所以掩膜里量到的最大索引
    要 +1 才等于标注坐标的右边界。
    """
    h, w = shape
    arr = np.zeros((h, w), dtype=np.uint8)
    arr[12:18, 20:28] = 1
    ann = Ann("box", [20.0, 12.0, 28.0, 18.0], "blob")

    out = arr.copy()
    for op_name, params in chain:
        out = clean.CLEAN_OPS.get(op_name).create(**params).apply(out)
    mapped, new_shape = clean.map_geometry(ann, chain, shape)

    ys, xs = np.nonzero(out)
    assert xs.size, "变换后掩膜不能空"
    assert mapped.coords == [float(xs.min()), float(ys.min()),
                             float(xs.max()) + 1, float(ys.max()) + 1]
    assert tuple(int(v) for v in new_shape) == out.shape


def test_map_geometry_moves_time_intervals_with_the_curve():
    ann = Ann("interval", [100.0, 140.0], "加热器")
    moved, _ = clean.map_geometry(ann, [("shift_time", {"dt": 12.5})], (10, 2))
    assert moved.coords == [112.5, 152.5]


def test_map_geometry_ignores_time_shift_for_spatial_boxes():
    ann = Ann("box", [1.0, 2.0, 3.0, 4.0], "x")
    same, _ = clean.map_geometry(ann, [("shift_time", {"dt": 5.0})], (8, 8))
    assert same.coords == [1.0, 2.0, 3.0, 4.0]


def test_map_geometry_rejects_non_geometry_ops():
    with pytest.raises(ValueError):
        clean.map_geometry(Ann("box", [0.0, 0.0, 1.0, 1.0]), [("blur", {})], (8, 8))


def test_transformed_annotation_still_wraps_the_object(store, tmp_path):
    """裁剪后的标注必须挂在裁剪后的图上，并且框里仍然是同一个目标。"""
    s, did, vid, root = store
    path = save(tmp_path / "d.png", blobs(seed=11, positions=((50, 40),), size=120))
    src = io.load_any(path)
    arr = np.asarray(src.load(), dtype=np.float64)
    ys, xs = np.nonzero(arr > 200)
    box = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))
    s.put_annotation(vid, path, Ann("box", [float(v) for v in box], "blob"),
                     role="annotator", state="submitted")

    report = clean.apply(s, vid, [{"name": "crop", "params": {"box": [20, 20, 110, 110]}}],
                         uris=[path], out_dir=root / "out", write_transformed=True)
    rows = [r for r in s.list_annotations(report["version"]) if name(r["item_uri"]) == "0000_d.png"]
    assert len(rows) == 1
    moved = rows[0]["ann"].coords
    assert moved[0] == pytest.approx(box[0] - 20) and moved[2] == pytest.approx(box[2] - 20)

    # D3：库内 uri 为相对路径，加载前先还原成绝对路径
    cut = np.asarray(io.load_any(s.to_abs(rows[0]["item_uri"])).load(), dtype=np.float64)
    patch = cut[int(moved[1]):int(moved[3]) + 1, int(moved[0]):int(moved[2]) + 1]
    assert (patch > 200).any(), "框里必须还套着那个目标"
