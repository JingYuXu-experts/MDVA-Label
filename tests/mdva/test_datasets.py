"""分类示例数据集：产物形态、跨模态联动与可复现性。

数据集本身就是引擎的能力说明书——每一类都刻意压一个不同的能力点，
所以这里既测生成器，也测"生成出来的东西能不能被读被画"。
"""

import json

import numpy as np
import pytest

from mdva import datasets, io, render
from mdva.core.model import Modality, Slice


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("datasets")
    datasets.build_all(root, seed=3, n_telemetry=2400, n_frames=6, n_scenes=6)
    return root


def _items(root, cat):
    return io.scan(str(root / cat))


# --------------------------------------------------------------------------- #
# 结构与可复现性
# --------------------------------------------------------------------------- #

def test_aerospace_covers_all_five_modalities(built):
    items = _items(built, "aerospace")
    kinds = {i.modality for i in items}
    assert kinds == {Modality.IMAGE.value, Modality.CUBE.value, Modality.SERIES.value,
                     Modality.EVENT.value, Modality.VIDEO.value}, \
        "标题写的是多模态平台，这一类必须五种模态同时在场"


def test_medical_and_cv_shapes(built):
    med = {i.modality for i in _items(built, "medical")}
    assert {Modality.VIDEO.value, Modality.SERIES.value, Modality.EVENT.value} <= med

    cv = _items(built, "cv_segmentation")
    assert {i.modality for i in cv} == {Modality.IMAGE.value}
    assert len(cv) == 6 * 5, "每个场景：原图 + 真值 + 三个模型的预测"
    assert repr(cv[0]).startswith("DataItem("), "调试与日志里 repr 不能抛异常"


def test_truth_files_are_sidecars_not_data_items(built):
    """真值表必须被扫描器认出是旁路文件，否则它会作为一路数据参与时间联动。"""
    for cat in ("aerospace", "medical", "cv_segmentation"):
        ids = {i.id for i in _items(built, cat)}
        assert "truth.json" not in ids
        assert not any(".calib." in i or ".meta." in i for i in ids)


def test_same_seed_reproduces_the_dataset(tmp_path):
    a = datasets.build_aerospace(tmp_path / "a", seed=11, n_telemetry=200, n_frames=3)
    b = datasets.build_aerospace(tmp_path / "b", seed=11, n_telemetry=200, n_frames=3)
    ta = json.loads((tmp_path / "a" / "aerospace" / "truth.json").read_text(encoding="utf-8"))
    tb = json.loads((tmp_path / "b" / "aerospace" / "truth.json").read_text(encoding="utf-8"))
    assert ta == tb
    assert a["truth"]["injected_anomalies"] == b["truth"]["injected_anomalies"]


def test_manifest_lists_every_category(built):
    manifest = json.loads((built / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["generated_by"] == "mdva.datasets"
    assert manifest["seed"] == 3
    assert set(manifest["categories"]) == set(datasets.CATEGORIES)
    for name, entry in manifest["categories"].items():
        assert entry["root"].endswith(name)
        assert entry["modalities"] == datasets.CATEGORIES[name]["modalities"]
        assert entry["truth"], f"{name} 没有写出真值，算法就没法量化评测"


def test_build_all_forwards_only_accepted_knobs(tmp_path):
    """三类生成器的可调参数不同，按签名过滤而不是要求统一签名。"""
    out = datasets.build_all(tmp_path, categories=["cv_segmentation"], seed=1,
                             n_telemetry=999, n_scenes=2)
    assert set(out) == {"cv_segmentation"}
    assert len(list((tmp_path / "cv_segmentation" / "images").glob("*.png"))) == 2
    assert not (tmp_path / "aerospace").exists()


# --------------------------------------------------------------------------- #
# 语义正确性
# --------------------------------------------------------------------------- #

def test_discrete_channel_keeps_enum_semantics(built):
    """工作模式这类枚举量绝不能被当连续量处理。"""
    item = next(i for i in _items(built, "aerospace")
                if i.modality == Modality.SERIES.value and i.id == "engineering.csv")
    assert "Payload_Mode" in item.meta["channels"]
    idx = item.meta["channels"].index("Payload_Mode")
    assert item.meta["discrete"][idx] is True
    assert item.meta["units"][idx] == ""


def test_events_align_with_the_telemetry_clock(built):
    items = {i.id: i for i in _items(built, "aerospace")}
    tele = items["engineering.csv"]
    ops = items["operations.json"]
    assert tele.t_ref <= ops.t_ref <= tele.t_end, "操作事件必须落在遥测时间窗内才能对齐"
    track = items["ground_track.csv"]
    assert track.t_ref == tele.t_ref


def test_solar_sequence_is_time_linkable(built):
    items = {i.id: i for i in _items(built, "aerospace")}
    seq = items["solar_evolution"]
    assert seq.meta["backend"] == "frame_dir"
    assert seq.t_ref is not None and seq.t_end > seq.t_ref
    # 帧间隔 2.5s，16 帧共 37.5s
    assert seq.t_end - seq.t_ref == pytest.approx((seq.n_views - 1) * 2.5)


def test_ct_stack_has_no_absolute_time_but_still_browses(built):
    """断层序列没有采集时刻：不被时间窗排除，但绝不按时间取帧。"""
    item = next(i for i in _items(built, "medical") if i.modality == Modality.VIDEO.value)
    assert item.t_ref is None
    assert item.n_views == 48
    assert item.meta["hu_intercept"] == -1024

    # 没有时间基准时只能按序号取帧：把窗口拖到任意时刻都应该还是第 3 层
    far = render.pick_slice(item, Slice(index=3, t0=1e12, t1=1e12 + 5.0))
    near = render.pick_slice(item, Slice(index=3, t0=0.0, t1=10.0))
    assert np.array_equal(far, near)


def test_pathology_gt_and_rgb_are_paired(built):
    ids = {i.id for i in _items(built, "medical")}
    assert {"slide_00_rgb.png", "slide_00_gt.png"} <= ids


def test_segmentation_models_fail_differently(built):
    """三个模型必须各有强弱，否则"并行对比"就没有信息量。"""
    truth = json.loads((built / "cv_segmentation" / "truth.json").read_text(encoding="utf-8"))
    per = truth["per_model"]
    assert set(per) == {"unet", "deeplabv3", "segformer_s"}
    assert all(0.0 < per[m]["mean_miou"] < 1.0 for m in per), "扰动没生效就会得到满分或零分"
    assert len({per[m]["mean_miou"] for m in per}) == 3

    # 名次不能一致：每个模型的强项类别不同，正是"必须并排看"的理由
    strong = {m: max(("blob", "stripe", "ring"), key=lambda c: per[m][f"mean_iou_{c}"]) for m in per}
    assert len(set(strong.values())) > 1, f"三个模型的强项完全相同：{strong}"

    ranking = sorted(per, key=lambda m: per[m]["mean_miou"], reverse=True)
    by_ring = sorted(per, key=lambda m: per[m]["mean_iou_ring"], reverse=True)
    assert ranking != by_ring, "换一类指标就应该改变结论，否则单指标就够了"


# --------------------------------------------------------------------------- #
# 能不能真的画出来
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cat", ["aerospace", "medical", "cv_segmentation"])
def test_every_generated_item_renders(built, cat):
    for item in _items(built, cat):
        res = render.render_item(item, size=(96, 96), window=Slice(index=1))
        rgb = np.asarray(res.rgb)
        assert rgb.shape == (96, 96, 3), item.id
        assert rgb.dtype == np.uint8
        assert res.title


def test_mosaic_of_a_category_produces_one_picture(built):
    items = _items(built, "aerospace")
    results = [render.render_item(i, size=(160, 120)) for i in items]
    img = render.render_mosaic(results, rows=2, cols=4, cell=(160, 120))
    assert img.ndim == 3 and img.shape[2] == 3
    assert img.shape[1] >= 4 * 160


def test_timeaxis_window_frames_four_modalities(built):
    """跨模态联动的核心断言：一个时间窗要同时框住遥测、指令事件、载荷图像和帧序列。"""
    truth = json.loads((built / "aerospace" / "truth.json").read_text(encoding="utf-8"))
    w0, w1 = truth["linkage_window"]
    inside = io.items_in_window(_items(built, "aerospace"), Slice.at_time(w0, w1))
    kinds = {i.modality for i in inside}
    assert {Modality.SERIES.value, Modality.EVENT.value,
            Modality.IMAGE.value, Modality.VIDEO.value} <= kinds
    assert Modality.CUBE.value not in kinds, "立方体排在另一次观测，这个窗不该框住它"
    assert all(i.t_ref is not None for i in inside)


def test_untimed_items_are_never_excluded_but_never_time_driven(built):
    """没有 t_ref 的项不参与联动定位，但也不该被时间窗判成"不在场"。"""
    ct = next(i for i in _items(built, "medical") if i.modality == Modality.VIDEO.value)
    assert ct.t_ref is None
    assert ct.overlaps(Slice.at_time(1e12, 1e12 + 1.0))
    assert ct.time_span == (float("-inf"), float("inf"))
