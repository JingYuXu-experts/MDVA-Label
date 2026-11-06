"""增强层：可重放的谱系、掩膜同步、时序保离散、学科策略拦截。

四条硬验收：
1. 同 seed 重放得到同一条链（`replay`）和同一批字节——论文里的增强配置必须可复现；
2. 掩膜与图像走完全相同的整数索引链，类别编号一个都不能变；
3. 时序增强不许碰离散通道，落盘的 CSV 再读回来不能二次标定；
4. 原件不动，增强是"加副本"，父版本的标注一条都不少。
"""

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from mdva import augment as AUG, clean, io
from mdva.annotate.model import Ann
from mdva.annotate.store import Store
from mdva.augment.lineage import Recipe, plan
from mdva.augment.ops import Spec, apply_image, apply_series, build, crop_box, draw
from mdva.core.model import TimeSeries


def blobs(size=120, positions=((30, 40),), amp=420.0, seed=0, noise=6.0):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    arr = rng.normal(30.0, noise, (size, size))
    for cx, cy in positions:
        arr += amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 6.0 ** 2))
    arr = (arr - arr.min()) / (arr.max() - arr.min()) * 255.0
    return np.clip(arr, 0, 255).astype(np.uint8)


def save(path, arr) -> str:
    Image.fromarray(np.ascontiguousarray(np.asarray(arr))).save(str(path))
    return str(path)


def name(uri) -> str:
    return Path(str(uri)).name


def sha(path) -> str:
    return hashlib.sha256(Path(str(path)).read_bytes()).hexdigest()


def stat(path) -> tuple:
    st = Path(str(path)).stat()
    return (st.st_size, st.st_mtime_ns)


# ---------------------------------------------------------------------- 夹具

@pytest.fixture
def field(tmp_path):
    """一张 120×120 的亮斑图 + 同名类别掩膜（编号只有 0 和 2）。"""
    img = blobs()
    png = save(tmp_path / "f0.png", img)
    mask = np.zeros(img.shape, dtype=np.uint8)
    mask[34:46, 24:38] = 2
    save(tmp_path / "f0_mask.png", mask)
    return {"png": png, "mask": str(tmp_path / "f0_mask.png"), "root": tmp_path}


@pytest.fixture
def telemetry(tmp_path):
    """600 点遥测：SA_Temp 连续（标定 scale=2/offset=5），SA_Mode 离散 0..5。"""
    t = np.arange(600, dtype=np.float64)
    values = np.column_stack([np.sin(t / 50.0) * 10.0 + 20.0, (t // 100).astype(float)])
    path = tmp_path / "tel.csv"
    rows = ["utc,SA_Temp,SA_Mode"]
    rows += [f"{t[i]:.3f},{values[i, 0]:.6f},{values[i, 1]:.6f}" for i in range(t.size)]
    path.write_text("\n".join(rows), encoding="utf-8")
    calib = {"SA_Temp": {"type": "analog", "scale": 2.0, "offset": 5.0, "unit": "V"},
             "SA_Mode": {"type": "discrete", "values": {str(k): f"S{k}" for k in range(6)}}}
    (tmp_path / "tel.calib.json").write_text(json.dumps(calib), encoding="utf-8")
    return str(path)


@pytest.fixture
def store(tmp_path, field, telemetry):
    s = Store(tmp_path / "mdva.sqlite")
    did = s.create_dataset("增强试验", "segmentation", tmp_path)
    vid = s.put_version(did, None, "init")
    s.set_labels(did, [{"name": "blob"}, {"name": "加热器"}])
    s.put_annotation(vid, field["png"], Ann("box", [24.0, 34.0, 38.0, 46.0], "blob"),
                     role="annotator", state="submitted")
    t0 = float(io.load_any(telemetry).t_ref)
    s.put_annotation(vid, telemetry, Ann("interval", [t0 + 100.0, t0 + 160.0], "加热器"),
                     role="annotator", state="submitted")
    yield s, did, vid, tmp_path
    s.close()


#: 裁剪留 0.9 边长，保证镜像后的亮斑不会被切掉（几何一致性测试要拿它当尺子）
RECIPE = {"seed": 7, "factor": 2, "ops": [
    {"name": "flip", "params": {"p_horizontal": 1.0, "p_vertical": 0.0}},
    {"name": "crop", "params": {"fraction": 0.9, "fraction_jitter": 0.0}},
    {"name": "photometric", "params": {"noise": 0.01}},
]}

GEOM_CHAINS = [
    [("flip", {"p_horizontal": 1.0, "p_vertical": 0.0})],
    [("flip", {"p_horizontal": 0.0, "p_vertical": 1.0})],
    [("rotate90", {"k_choices": [1], "p": 1.0})],
    [("rotate90", {"k_choices": [3], "p": 1.0})],
    [("crop", {"fraction": 0.5, "fraction_jitter": 0.0, "offset": 0.0})],
    [("rotate90", {"k_choices": [2], "p": 1.0}),
     ("flip", {"p_horizontal": 1.0, "p_vertical": 1.0})],
]


# ---------------------------------------------------------------------- 配方与排产

def test_recipe_accepts_names_lists_and_objects():
    assert Recipe.from_obj(["flip"]).ops == [{"name": "flip"}]
    assert Recipe.from_obj({"ops": ["crop"]}).ops == [{"name": "crop"}]
    assert Recipe.from_obj(Recipe(seed=3, ops=[{"name": "crop"}])).seed == 3
    with pytest.raises(ValueError):
        Recipe.from_obj({"seed": 1})
    with pytest.raises(TypeError):
        Recipe.from_obj(3)


def test_recipe_round_trips_through_json():
    recipe = Recipe.from_obj(RECIPE)
    assert Recipe.from_obj(recipe.to_json()).to_json() == recipe.to_json()


def test_plan_is_deterministic_regardless_of_input_order(field, tmp_path):
    items = [io.load_any(save(tmp_path / f"x{k}.png", blobs(seed=k))) for k in range(3)]
    recipe = Recipe.from_obj({"seed": 11, "factor": 2, "ops": ["flip", "photometric"]})
    forward = {name(j.uri): [s.to_json() for s in j.specs] for j in plan(items, recipe)}
    shuffled = plan(list(reversed(items)), recipe)
    assert forward == {name(j.uri): [s.to_json() for s in j.specs] for j in shuffled}
    assert len(shuffled) == 6                                   # 3 张 × factor 2


def test_plan_different_seed_different_chain(field):
    item = io.load_any(field["png"])
    one = [j.to_json() for j in plan([item], Recipe.from_obj(RECIPE))]
    two = [j.to_json() for j in plan([item], Recipe.from_obj({**RECIPE, "seed": 8}))]
    assert one != two
    assert one[0]["specs"][2]["params"]["seed"] != two[0]["specs"][2]["params"]["seed"]


def test_plan_skips_items_no_op_can_handle(field, telemetry):
    assert plan([io.load_any(field["png"])],
                Recipe.from_obj({"ops": ["series_jitter"]})) == []
    both = plan([io.load_any(field["png"]), io.load_any(telemetry)],
                Recipe.from_obj({"seed": 1, "factor": 1, "ops": ["series_jitter"]}))
    assert [name(j.uri) for j in both] == ["tel.csv"]


def test_plan_limit_caps_the_sources(field, tmp_path):
    items = [io.load_any(save(tmp_path / f"y{k}.png", blobs(seed=k))) for k in range(4)]
    jobs = plan(items, Recipe.from_obj({"seed": 0, "factor": 2, "limit": 3, "ops": ["flip"]}))
    assert len({j.uri for j in jobs}) == 3 and len(jobs) == 6


def test_spec_round_trips_through_json():
    op = build(["photometric"])[0]
    spec = Spec(op.name, op.sample(np.random.default_rng(0)))
    assert Spec.from_json(spec.to_json()).params == spec.params


# ---------------------------------------------------------------------- 单算子

def test_photometric_leaves_the_mask_and_the_nan_pattern_alone():
    img = blobs()
    mask = (img > 200).astype(np.uint8)
    specs = draw(build([{"name": "photometric", "params": {"noise": 0.05, "contrast": 0.2}}]),
                 np.random.default_rng(1))
    out, out_mask, chain = apply_image(img.astype(np.float64), mask, specs)
    assert chain == [], "光度扰动不该产生几何链"
    assert np.array_equal(out_mask, mask), "光度扰动对掩膜必须是恒等"
    assert not np.allclose(out, img)


def test_photometric_preserves_nonfinite_pixels(tmp_path):
    from mdva import synth

    arr = blobs().astype(np.float32)
    arr[5:9, 5:9] = np.nan
    path = str(synth.write_fits(tmp_path / "nan.fits", arr, {"INSTRUME": "T"}))
    item = io.load_any(path)
    src = np.asarray(item.load(), dtype=np.float64)
    specs = draw(build([{"name": "photometric", "params": {"noise": 0.02}}]),
                 np.random.default_rng(3))
    out, _m, _c = apply_image(src, None, specs, item.modality)
    assert np.array_equal(~np.isfinite(out), ~np.isfinite(src)), "NaN 位置不许被填平"


@pytest.mark.parametrize("chain_spec", GEOM_CHAINS)
def test_geometric_ops_apply_and_chain_agree_on_the_mask(chain_spec):
    """`apply` 切出来的像素和 `chain` 换算出的标注必须落在同一处，用掩膜当尺子。

    参数一律由 `draw` 抽样得到（这些配置的 p=1、jitter=0，抽出来是确定的），
    因为手填 recipe 参数不等于增强时真正用的那份数。
    """
    h, w = 40, 60
    arr = np.zeros((h, w), dtype=np.uint8)
    arr[12:18, 20:28] = 1
    ann = Ann("box", [20.0, 12.0, 28.0, 18.0], "blob")
    specs = draw(build([{"name": n, "params": dict(p)} for n, p in chain_spec]),
                 np.random.default_rng(0))
    out, out_mask, chain = apply_image(arr, arr.copy(), specs)
    mapped, _ = clean.map_geometry(ann, chain, (h, w))

    ys, xs = np.nonzero(out)
    assert xs.size, "变换后掩膜不能空"
    assert np.array_equal(out, out_mask), "掩膜必须走和图像完全相同的索引链"
    assert mapped.coords == [float(xs.min()), float(ys.min()),
                             float(xs.max()) + 1, float(ys.max()) + 1]


def test_crop_box_stays_inside_the_image():
    for frac in (1.0, 0.8, 0.5, 0.1):
        x0, y0, x1, y1 = crop_box((60, 100), frac, ox=0.7, oy=-0.4)
        assert 0 <= x0 < x1 <= 100 and 0 <= y0 < y1 <= 60
        assert x1 - x0 == max(1, round(100 * frac))
    assert crop_box((20, 20), 1.0) == (0, 0, 20, 20)


def test_geometric_chain_is_lossless_on_label_maps():
    arr = np.arange(12, dtype=np.uint8).reshape(3, 4)
    specs = draw(build(["rotate90",
                        {"name": "flip", "params": {"p_horizontal": 1.0, "p_vertical": 1.0}}]),
                 np.random.default_rng(0))
    out, _m, chain = apply_image(arr, arr.copy(), specs)
    assert sorted(np.unique(out).tolist()) == list(range(12))   # 整数索引，不产生新编号
    assert [op for op, _p in chain] == ["rotate", "mirror", "mirror"]


def test_series_jitter_never_touches_discrete_channels(telemetry):
    ts = io.load_any(telemetry).load()
    specs = draw(build([{"name": "series_jitter", "params": {"sigma": 0.05, "gain": 0.2}}]),
                 np.random.default_rng(5))
    out, chain = apply_series(ts, specs)
    assert chain == [], "series_jitter 不挪时间，几何链应为空"
    assert np.array_equal(out.values[:, 1], ts.values[:, 1]), "离散通道一个码值都不许变"
    assert not np.allclose(out.values[:, 0], ts.values[:, 0])
    assert out.channels == ts.channels and out.discrete == ts.discrete


def test_series_jitter_keeps_nan_positions(telemetry):
    ts = io.load_any(telemetry).load()
    hole = ts.values.copy()
    hole[10, 0] = np.nan
    broken = TimeSeries(t=ts.t, values=hole, channels=ts.channels, units=ts.units,
                        discrete=ts.discrete)
    out, _ = apply_series(broken, draw(build(["series_jitter"]), np.random.default_rng(2)))
    assert np.isnan(out.values[10, 0])


def test_series_offset_moves_the_curve_and_the_labels_together(telemetry):
    ts = io.load_any(telemetry).load()
    specs = draw(build([{"name": "series_offset", "params": {"max_shift": 40.0}}]),
                 np.random.default_rng(9))
    out, chain = apply_series(ts, specs)
    dt = float(specs[0].params["dt"])
    assert np.allclose(out.t - ts.t, dt)
    assert np.array_equal(out.values, ts.values)
    assert chain == [("shift_time", {"dt": dt})]

    ann = Ann("interval", [float(ts.t[0]) + 100.0, float(ts.t[0]) + 160.0], "加热器")
    moved, _ = clean.map_geometry(ann, chain, ts.values.shape)
    assert moved.coords == [pytest.approx(ann.coords[0] + dt),
                            pytest.approx(ann.coords[1] + dt)]


def test_apply_image_ignores_ops_of_the_wrong_modality():
    arr = np.zeros((10, 10), dtype=np.uint8)
    specs = draw(build(["series_jitter",
                        {"name": "flip", "params": {"p_horizontal": 1.0}}]),
                 np.random.default_rng(0))
    out, _m, chain = apply_image(arr, None, specs)
    assert [op for op, _p in chain] == ["mirror"]               # 时序算子不掺和图像


# ---------------------------------------------------------------------- 执行

def test_run_writes_copies_and_keeps_originals(store, field):
    s, did, vid, root = store
    watched = {k: v for k, v in field.items() if k != "root"}
    before = {k: stat(v) for k, v in watched.items()}
    report = AUG.run(s, vid, RECIPE, uris=[field["png"]], out_dir=root / "out")
    assert report["summary"]["n_generated"] == 2
    assert report["summary"]["n_masks"] == 2
    assert report["summary"]["n_files"] == 4
    assert all(Path(r["written"]).is_file() for r in report["written"])
    assert name(report["written"][0]["written"]) == "0000_f0_r0.png"
    assert {k: stat(v) for k, v in watched.items()} == before   # 原件字节与时间都没动


def test_run_inherits_originals_and_maps_the_copy_labels(store, field):
    s, did, vid, root = store
    report = AUG.run(s, vid, RECIPE, uris=[field["png"]], out_dir=root / "out")
    rows = s.list_annotations(report["version"])
    by_uri = {}
    for r in rows:
        by_uri.setdefault(name(r["item_uri"]), []).append(r["ann"].coords)
    assert by_uri["f0.png"] == [[24.0, 34.0, 38.0, 46.0]]         # 原件标注原样在
    assert by_uri["tel.csv"] == [[100.0, 160.0]], "没参与增强的条目也要继承过来"
    assert len(by_uri) == 4                                      # 2 个原件 + 2 个副本
    assert {r["state"] for r in rows} == {"submitted"}
    assert {r["role"] for r in rows} == {"annotator"}
    for rec in report["written"]:                              # 副本的框必须还在副本图上
        for r in rows:
            if r["item_uri"] == rec["written"]:
                assert 0 <= r["ann"].coords[0] and r["ann"].coords[2] <= rec["width"] + 1
                assert 0 <= r["ann"].coords[1] and r["ann"].coords[3] <= rec["height"] + 1


def test_run_mask_labels_survive_the_chain(store, field):
    s, did, vid, root = store
    report = AUG.run(s, vid, RECIPE, uris=[field["png"]], out_dir=root / "out")
    src = np.asarray(Image.open(field["mask"]))
    for rec in report["written"]:
        out = np.asarray(Image.open(rec["aux"]["mask"]["written"]))
        assert sorted(np.unique(out).tolist()) == sorted(np.unique(src).tolist()), \
            "掩膜类别编号不能因为增强多出或缺少数值"
        # D3：库内 uri 为相对路径，rec["written"] 是引擎侧绝对路径，比对前先归一
        ann = [r["ann"] for r in s.list_annotations(report["version"])
               if r["item_uri"] == s.to_rel(rec["written"])][0]
        ys, xs = np.nonzero(out)
        assert ann.coords == [float(xs.min()), float(ys.min()),
                             float(xs.max()) + 1, float(ys.max()) + 1], \
            "增强后的框必须还套着掩膜里那块目标"


def test_same_seed_produces_byte_identical_images(store, field):
    s, did, vid, root = store
    a = AUG.run(s, vid, RECIPE, uris=[field["png"]], out_dir=root / "out")
    b = AUG.run(s, vid, RECIPE, uris=[field["png"]], out_dir=root / "out2")
    da = {name(r["written"]): sha(r["written"]) for r in a["written"]}
    db = {name(r["written"]): sha(r["written"]) for r in b["written"]}
    assert len(da) == 2 and da == db, "同 seed 同配方生成的文件必须 sha 逐一对上"
    assert a["written"][0]["specs"] == b["written"][0]["specs"]


def test_different_seed_produces_different_pixels(store, field):
    s, did, vid, root = store
    a = AUG.run(s, vid, RECIPE, uris=[field["png"]], out_dir=root / "out")
    b = AUG.run(s, vid, {**RECIPE, "seed": 99}, uris=[field["png"]], out_dir=root / "out")
    assert sha(a["written"][0]["written"]) != sha(b["written"][0]["written"])


def test_run_series_round_trips_without_double_calibration(store, telemetry):
    s, did, vid, root = store
    recipe = {"seed": 3, "factor": 1,
              "ops": [{"name": "series_jitter", "params": {"sigma": 0.02, "gain": 0.1}},
                      {"name": "series_offset", "params": {"max_shift": 10.0}}]}
    report = AUG.run(s, vid, recipe, uris=[telemetry], out_dir=root / "out")
    rec = report["written"][0]
    ts = io.load_any(rec["written"]).load()
    src = io.load_any(telemetry).load()

    assert ts.channels == ["SA_Temp", "SA_Mode"]
    assert ts.discrete == [False, True], "侧车丢了离散标记，插值和求均值就会造假工况"
    assert np.array_equal(ts.values[:, 1], src.values[:, 1])
    assert not np.allclose(ts.values[:, 0], src.values[:, 0])
    # 若侧车沿用了原表的 scale=2/offset=5，均值会整体翻倍——这条断言就是抓二次标定的
    span = float(src.values[:, 0].max() - src.values[:, 0].min())
    assert abs(float(np.mean(ts.values[:, 0])) - float(np.mean(src.values[:, 0]))) < 0.1 * span
    assert Path(rec["aux"]["sidecar"]["written"]).is_file()


def test_run_series_shifts_the_interval_annotation(store, telemetry):
    s, did, vid, root = store
    recipe = {"seed": 4, "factor": 1,
              "ops": [{"name": "series_offset", "params": {"max_shift": 25.0}}]}
    report = AUG.run(s, vid, recipe, uris=[telemetry], out_dir=root / "out")
    rec = report["written"][0]
    dt = float(rec["chain"][0]["params"]["dt"])
    t_ref = float(io.load_any(telemetry).t_ref)
    # D3：库内 uri 为相对路径，rec["written"] 是引擎侧绝对路径，比对前先归一
    rows = [r for r in s.list_annotations(report["version"])
            if r["item_uri"] == s.to_rel(rec["written"])]
    assert rows[0]["ann"].coords == [pytest.approx(t_ref + 100.0 + dt),
                                     pytest.approx(t_ref + 160.0 + dt)]


def test_run_refuses_when_nothing_can_be_augmented(store, field):
    s, did, vid, root = store
    with pytest.raises(ValueError):
        AUG.run(s, vid, {"ops": ["series_jitter"]}, uris=[field["png"]], out_dir=root / "out")


def test_rollback_deletes_masks_and_keeps_originals(store, field):
    s, did, vid, root = store
    report = AUG.run(s, vid, RECIPE, uris=[field["png"]], out_dir=root / "out")
    files = sorted([r["written"] for r in report["written"]] +
                   [r["aux"]["mask"]["written"] for r in report["written"]])
    back = clean.rollback(s, report["version"])
    assert sorted(back["removed_files"]) == files
    assert all(not Path(p).exists() for p in files)
    assert Path(field["png"]).is_file() and Path(field["mask"]).is_file()
    assert back["op"] == "augment" and back["restored"] == vid
    assert [r["ann"].coords for r in s.list_annotations(back["new_version"])] == \
        [r["ann"].coords for r in s.list_annotations(vid)]


def test_rollback_keeps_mask_someone_else_touched(store, field):
    s, did, vid, root = store
    report = AUG.run(s, vid, RECIPE, uris=[field["png"]], out_dir=root / "out")
    mask = report["written"][0]["aux"]["mask"]["written"]
    Path(mask).write_bytes(b"not ours any more")
    back = clean.rollback(s, report["version"])
    assert mask not in back["removed_files"]
    assert any(m["written"] == mask for m in back["skipped_files"])
    assert Path(mask).is_file()


# ---------------------------------------------------------------------- 谱系重放

def test_replay_matches_a_fresh_run(store, field):
    s, did, vid, root = store
    report = AUG.run(s, vid, RECIPE, uris=[field["png"]], out_dir=root / "out")
    out = AUG.replay(s, report["version"])
    assert out["identical"] and out["n_jobs"] == 2 and out["diff"] == []


def test_replay_catches_a_recipe_that_no_longer_fits(store, field):
    """谱系与配方对不上时必须报差异——否则「可复现」只是口号。"""
    s, did, vid, root = store
    report = AUG.run(s, vid, RECIPE, uris=[field["png"]], out_dir=root / "out")
    tampered = s.put_version(did, vid, "augment", {"recipe": {**RECIPE, "seed": 1234}})
    task = s.create_task("augment", did, vid, tampered, {})
    s.update_task(task, "done", {"written": report["written"]}, version_out=tampered)
    out = AUG.replay(s, tampered)
    assert not out["identical"] and out["diff"]


def test_replay_requires_an_augment_version(store):
    s, did, vid, root = store
    with pytest.raises(ValueError):
        AUG.replay(s, vid)


def test_read_lineage_comes_from_the_database(store, field):
    s, did, vid, root = store
    report = AUG.run(s, vid, RECIPE, uris=[field["png"]], out_dir=root / "out")
    stored = AUG.read_lineage(s, report["version"])
    assert [r["written"] for r in stored] == [r["written"] for r in report["written"]]
    assert (Path(report["out_dir"]) / "lineage.json").is_file()


# ---------------------------------------------------------------------- 学科策略

def test_presets_are_exposed_with_reasons():
    listing = AUG.presets()
    assert [p["name"] for p in listing] == ["aerospace", "medical", "cv_segmentation", "series"]
    assert all(p["reason"] and p["recipe"]["ops"] for p in listing)
    with pytest.raises(KeyError):
        AUG.get("nope")


def test_suggest_prefers_category_then_task():
    assert AUG.suggest({"meta": {"category": "aerospace"}, "type": "detection"})["name"] \
        == "aerospace"
    assert AUG.suggest({"type": "series_interval"})["name"] == "series"
    assert AUG.suggest({"type": "segmentation"})["name"] == "cv_segmentation"
    assert AUG.suggest(None)["name"] == "cv_segmentation"


def test_suggest_uses_modality_when_dataset_is_untyped(field, telemetry):
    assert AUG.suggest({"type": "mixed"}, [io.load_any(telemetry)])["name"] == "series"
    assert AUG.suggest({"type": "mixed"}, [io.load_any(field["png"])])["name"] \
        == "cv_segmentation"


def test_check_flags_the_recipes_that_break_a_dataset(field, telemetry):
    images = [io.load_any(field["png"])]
    assert AUG.check(RECIPE, images) == []

    vertical = {"seed": 0, "factor": 2,
                "ops": [{"name": "flip", "params": {"p_vertical": 0.5}}]}
    assert any("上下镜像" in w for w in AUG.check(vertical, images, category="aerospace"))
    assert AUG.check(vertical, images, category="medical") == []

    tiny = {"seed": 0, "factor": 2, "ops": [{"name": "crop", "params": {"fraction": 0.3}}]}
    assert any("靠边" in w for w in AUG.check(tiny, images))

    too_much = {**RECIPE, "factor": 8}
    assert any("占大头" in w for w in AUG.check(too_much, images))

    wrong = {"seed": 0, "ops": ["series_jitter"]}
    assert any("处理不了" in w for w in AUG.check(wrong, images))
    assert any("离散通道" in w for w in AUG.check(wrong, [io.load_any(telemetry)]))


def test_presets_pass_their_own_check(field, telemetry):
    """预设得对自己的模态说得过去，不能「一键增强」变成「一键报错」。"""
    images, series = [io.load_any(field["png"])], [io.load_any(telemetry)]
    for preset in ("aerospace", "medical", "cv_segmentation"):
        recipe = AUG.get(preset)["recipe"]
        assert not any("处理不了" in w for w in AUG.check(recipe, images))
    assert not any("处理不了" in w for w in AUG.check(AUG.get("series")["recipe"], series))
    assert not any("上下镜像" in w for w in
                   AUG.check(AUG.get("aerospace")["recipe"], images, category="aerospace"))
