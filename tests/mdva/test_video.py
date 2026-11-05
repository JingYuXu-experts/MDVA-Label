"""视频 / 帧序列模态：三种后端、时间基准、与扫描器的折叠规则。"""

import json

import numpy as np
import pytest

from mdva import io
from mdva.core.model import Modality, Slice
from mdva.io import video as V


def _write_frames(directory, n=6, shape=(16, 24)):
    from PIL import Image

    directory.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        arr = np.full(shape, i * 30 % 256, dtype=np.uint8)
        Image.fromarray(arr).save(str(directory / f"frame_{i:04d}.png"))
    return directory


# --------------------------------------------------------------------------- #
# 目录帧序列
# --------------------------------------------------------------------------- #

def test_directory_frames_sort_naturally(tmp_path):
    _write_frames(tmp_path / "seq", n=12)
    src = V.DirectoryFrames(tmp_path / "seq")
    assert src.n_frames == 12
    # frame_2 必须排在 frame_10 之前，否则时间轴会乱序
    names = [p.name for p in src.paths]
    assert names.index("frame_0002.png") < names.index("frame_0010.png")


def test_directory_frame_values_follow_index(tmp_path):
    _write_frames(tmp_path / "seq", n=6)
    src = V.DirectoryFrames(tmp_path / "seq")
    assert [int(src.frame(i).mean()) for i in range(6)] == [0, 30, 60, 90, 120, 150]


def test_directory_without_sidecar_is_not_auto_collapsed(tmp_path):
    """一目录图像默认是"多张图分别浏览"，只有自带 meta.json 才被扫描折叠成视频。"""
    d = _write_frames(tmp_path / "plain", n=5)
    assert V.is_frame_dir(d) is False
    assert io.sniff(str(d)) == ""
    assert len(io.scan(d)) == 5


def test_explicitly_named_directory_still_plays(tmp_path):
    """扫描保守，但用户在文件选择框里点名一个目录时，就是要按序列播。"""
    d = _write_frames(tmp_path / "plain", n=5)
    item = io.load_any(str(d))
    assert item.modality == Modality.VIDEO.value
    assert item.n_views == 5


def test_directory_frames_reject_empty_dir(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError):
        V.DirectoryFrames(tmp_path / "empty")


def test_frame_time_index_roundtrip(tmp_path):
    d = _write_frames(tmp_path / "seq", n=8)
    (d / "meta.json").write_text(
        json.dumps({"fps": 2.0, "t0": 1000.0}), encoding="utf-8")
    src = V.make_source(d)
    assert src.fps == 2.0
    assert src.t0 == 1000.0
    assert src.time_of(3) == pytest.approx(1000.0 + 1.5)
    assert src.index_of(1001.0) == 2
    assert src.index_of(1e9) == 7, "越界时刻应夹到最后一帧而不是抛异常"
    assert src.duration == pytest.approx(3.5)


def test_t0_from_names_is_relative_and_not_linked(tmp_path):
    """没有 meta.json 时帧名里的时刻只是相对秒，不能当绝对时间参与联动。"""
    from PIL import Image

    d = tmp_path / "rel"
    d.mkdir()
    for i in range(4):
        Image.fromarray(np.zeros((8, 8), np.uint8)).save(str(d / f"{i * 2.5:.1f}s_frame_1.png"))
    src = V.make_source(d)
    assert src.t0 == pytest.approx(0.0)
    item = V.load_video(d)
    assert item.t_ref == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# npy 帧栈
# --------------------------------------------------------------------------- #

def test_npy_stack_is_lazy_and_shape_aware(tmp_path):
    stack = np.stack([np.full((10, 12), i, np.uint16) for i in range(5)])
    path = tmp_path / "stack.npy"
    np.save(path, stack)
    src = V.make_source(path)
    assert isinstance(src, V.NpyStackFrames)
    assert src.n_frames == 5 and src.size == (10, 12)
    assert int(src.frame(3).mean()) == 3


def test_sniff_splits_npy_by_dimension(tmp_path):
    """同一扩展名两种用途：(n, ch) 是遥测，(t, y, x) 才是帧栈。"""
    twod = tmp_path / "table.npy"
    np.save(twod, np.zeros((50, 3)))
    assert io.sniff(str(twod)) == Modality.SERIES.value

    threed = tmp_path / "movie.npy"
    np.save(threed, np.zeros((8, 16, 16)))
    assert io.sniff(str(threed)) == Modality.VIDEO.value

    oned = tmp_path / "single.npy"
    np.save(oned, np.zeros(20))
    assert io.sniff(str(oned)) == Modality.SERIES.value


def test_npy_stack_rejects_2d(tmp_path):
    path = tmp_path / "bad.npy"
    np.save(path, np.zeros((4, 4)))
    with pytest.raises(ValueError):
        V.NpyStackFrames(path)


def test_sidecar_hu_metadata_reaches_item_meta(tmp_path):
    stack = np.stack([np.full((8, 8), i, np.uint16) for i in range(4)])
    path = tmp_path / "ct.npy"
    np.save(path, stack)
    path.with_name("ct.meta.json").write_text(
        json.dumps({"fps": 1.0, "hu_intercept": -1024, "n_slices": 4}), encoding="utf-8")
    item = io.load_any(str(path))
    assert item.meta["hu_intercept"] == -1024
    assert item.meta["backend"] == "npy_stack"


# --------------------------------------------------------------------------- #
# 视频文件
# --------------------------------------------------------------------------- #

def test_video_file_frame_count_is_exact_not_estimated(tmp_path):
    """GIF 的 duration 是每帧毫秒，按"整段秒数×fps"推算会多出一个数量级。"""
    imageio = pytest.importorskip("imageio")
    frames = [(np.ones((8, 8, 3)) * (i * 40)).astype(np.uint8) for i in range(6)]
    path = tmp_path / "clip.gif"
    imageio.mimsave(str(path), frames, duration=200)  # 每帧 200ms == 5fps

    src = V.make_source(path)
    assert src.n_frames == 6
    # 顺序、跳跃、回退三种访问方式必须拿到同一帧
    for i in (0, 1, 2, 5, 3, 4, 0):
        assert int(src.frame(i).mean()) == i * 40


def test_video_file_without_t0_does_not_claim_a_time_span(tmp_path):
    imageio = pytest.importorskip("imageio")
    path = tmp_path / "clip.gif"
    imageio.mimsave(str(path), [(np.ones((8, 8, 3)) * i).astype(np.uint8) for i in range(3)])
    item = io.load_any(str(path))
    assert item.modality == Modality.VIDEO.value
    assert item.t_ref is None
    assert item.time_span == (float("-inf"), float("inf"))
    assert item.n_views == 3


def test_explicit_t0_links_video_into_the_time_axis(tmp_path):
    imageio = pytest.importorskip("imageio")
    path = tmp_path / "clip.gif"
    imageio.mimsave(str(path), [(np.ones((8, 8, 3)) * i).astype(np.uint8) for i in range(4)], duration=500)
    # 容器里没有绝对时刻，t0 只能由调用方或侧车给出
    path.with_name("clip.meta.json").write_text(json.dumps({"fps": 2.0}), encoding="utf-8")
    item = io.load_any(str(path), t0=5000.0)
    assert item.t_ref == 5000.0
    assert item.t_end == pytest.approx(5001.5)
    assert item.overlaps(Slice.at_time(5001.0, 5002.0))
    assert not item.overlaps(Slice.at_time(6000.0, 7000.0))


# --------------------------------------------------------------------------- #
# 帧访问 / 导出 / 显示映射
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("index,expected", [(0, 0), (2, 60), (99, 150), (-5, 0)])
def test_frame_index_is_clipped(tmp_path, index, expected):
    d = _write_frames(tmp_path / "seq", n=6)
    item = io.load_any(str(d))
    assert int(V.frame_at(item, index).mean()) == expected


def test_time_to_frame_and_frame_time_agree(tmp_path):
    d = _write_frames(tmp_path / "seq", n=6)
    (d / "meta.json").write_text(json.dumps({"fps": 4.0, "t0": 200.0}), encoding="utf-8")
    item = io.load_any(str(d))
    for i in range(6):
        t = V.frame_time(item, i)
        assert V.time_to_frame(item, t) == i


def test_to_display_uint8_is_monotonic_and_spans_full_range():
    arr = np.array([[1000, 2000], [3000, 9000]], dtype=np.uint16)
    out = V.to_display_uint8(arr)
    assert out.dtype == np.uint8
    assert out[0, 0] == 0 and out[1, 1] == 255
    assert out[0, 1] < out[1, 0], "映射必须单调"


def test_to_display_uint8_handles_nan_and_flat_input():
    flat = V.to_display_uint8(np.full((4, 4), 7.0))
    assert flat.min() == flat.max() == 0, "无动态范围时画全黑而不是随机值"
    with_nan = V.to_display_uint8(np.array([[np.nan, 1.0], [2.0, 3.0]]))
    assert with_nan.shape == (2, 2)
    assert with_nan[1, 1] == 255
    assert with_nan[0, 0] == 0, "NaN 必须落到背景值，不能回绕成随机灰度"
    assert with_nan.dtype == np.uint8


def test_export_frames_writes_requested_window(tmp_path):
    d = _write_frames(tmp_path / "seq", n=10)
    item = io.load_any(str(d))
    out = tmp_path / "out"
    # 区间左闭右开，与 Slice 的时间窗口一致
    written = V.export_frames(item, out, start=2, stop=8, step=2)
    assert [p.name for p in sorted(out.iterdir())] == [f"{i:06d}.png" for i in (2, 4, 6)]
    assert len(written) == 3
    from PIL import Image

    assert Image.open(str(out / "000004.png")).size == (24, 16)


# --------------------------------------------------------------------------- #
# 与扫描器 / 渲染层的集成
# --------------------------------------------------------------------------- #

def test_scan_collapses_frame_dir_and_does_not_double_list_frames(tmp_path):
    d = _write_frames(tmp_path / "root" / "seq", n=6)
    (d / "meta.json").write_text(json.dumps({"fps": 1.0, "t0": 0.0}), encoding="utf-8")
    _write_frames(tmp_path / "root" / "loose", n=3)

    items = io.scan(tmp_path / "root")
    by_mod = {}
    for i in items:
        by_mod.setdefault(i.modality, []).append(i)
    assert len(by_mod.get(Modality.VIDEO.value, [])) == 1
    assert by_mod[Modality.VIDEO.value][0].n_views == 6
    assert len(by_mod[Modality.IMAGE.value]) == 3, "loose 目录仍按单张图逐项展开"


def test_scan_accepts_a_frame_dir_as_root(tmp_path):
    d = _write_frames(tmp_path / "seq", n=5)
    (d / "meta.json").write_text(json.dumps({"fps": 1.0, "t0": 10.0}), encoding="utf-8")
    items = io.scan(d)
    assert len(items) == 1 and items[0].modality == Modality.VIDEO.value


def test_video_frame_renders_in_color_not_pseudocolor(tmp_path):
    from PIL import Image

    d = tmp_path / "rgbseq"
    d.mkdir()
    for i in range(4):
        arr = np.zeros((12, 12, 3), np.uint8)
        arr[:, :, 0] = 40 + i * 50
        arr[:, :, 2] = 200 - i * 40
        Image.fromarray(arr).save(str(d / f"frame_{i:02d}.png"))
    (d / "meta.json").write_text(json.dumps({"fps": 1.0}), encoding="utf-8")

    from mdva import render

    item = io.load_any(str(d))
    res = render.render_item(item, size=(48, 48), window=Slice(index=2))
    rgb = np.asarray(res.rgb)
    assert rgb.shape == (48, 48, 3)
    assert res.info["colormap"] == "native"
    assert not np.array_equal(rgb[:, :, 0], rgb[:, :, 2]), "彩色帧不能被抹成灰度"
    assert "帧 2/4" in res.subtitle
