import numpy as np
import pytest

from mdva import render
from mdva.core.model import DataItem, EventRow, EventTable, Modality, Slice, TimeSeries
from mdva.render import overlay as OV


def _arr(h=64, w=128, dtype=np.uint16):
    a = np.linspace(0, 60000, h * w, dtype=np.float64).reshape(h, w)
    return a.astype(dtype)


def test_fit_size_keeps_aspect_ratio():
    assert render.fit_size(100, 100, 200, 100) == (100, 100)
    assert render.fit_size(100, 200, 200, 200) == (200, 100)
    assert render.fit_size(0, 0, 10, 10) == (10, 10)


def test_render_array_output_shape_and_dtype():
    rgb, info = render.render_array(_arr(), size=(120, 60), interval="minmax", stretch="linear")
    assert rgb.shape == (60, 120, 3)
    assert rgb.dtype == np.uint8
    assert info["vmax"] > info["vmin"]


def test_render_array_pads_instead_of_stretching_non_square():
    """2:1 的图放进 1:1 的格心应居中留边，不能把像元拉变形。"""
    rgb, info = render.render_array(_arr(h=64, w=128), size=(100, 100), keep_aspect=True)
    assert rgb.shape == (100, 100, 3)
    x0, y0, w, h, scale = info["fit"]
    assert w == 2 * h
    assert x0 == (100 - w) // 2
    assert scale == pytest.approx(100 / 128)


def test_render_array_without_aspect_lock_fills_cell():
    rgb, _ = render.render_array(_arr(h=64, w=128), size=(100, 50), keep_aspect=False)
    assert rgb.shape == (50, 100, 3)


def test_render_array_handles_nan_region():
    a = _arr(h=32, w=32).astype(float)
    a[:8, :8] = np.nan
    rgb, info = render.render_array(a, size=(32, 32), interval="percent")
    assert rgb.shape == (32, 32, 3)
    # 坏像元画成背景值，与有效区的亮部区分开
    assert rgb[0, 0].mean() < rgb[-1, -1].mean()


def test_pick_slice_indexes_and_clamps():
    arr = np.stack([np.full((8, 8), i, dtype=np.uint16) for i in range(5)])
    item = DataItem(id="c", modality=Modality.CUBE.value, uri="c.fits", shape=arr.shape,
                    n_views=5, _opener=lambda: arr)
    assert render.pick_slice(item, Slice.at_index(2))[0, 0] == 2
    assert render.pick_slice(item, Slice.at_index(99))[0, 0] == 4
    assert render.pick_slice(item, Slice.at_index(-1))[0, 0] == 0


def test_pick_slice_maps_time_onto_cube_axis():
    """时间驱动的切片：t_ref~t_end 线性映射到切片序号，这是联动的基础。"""
    arr = np.stack([np.full((4, 4), i, dtype=np.uint16) for i in range(10)])
    item = DataItem(id="c", modality=Modality.CUBE.value, uri="c.fits", shape=arr.shape,
                    n_views=10, t_ref=0.0, t_end=100.0, _opener=lambda: arr)
    mid = render.pick_slice(item, Slice.at_time(50.0, 60.0))
    assert mid[0, 0] in (4, 5)
    assert render.pick_slice(item, Slice.at_time(0.0, 1.0))[0, 0] == 0


def test_render_item_image():
    item = DataItem(id="i", modality=Modality.IMAGE.value, uri="i.fits", shape=(64, 64),
                    _opener=lambda: _arr(h=64, w=64).astype(float))
    r = render.render_item(item, size=(90, 60), style={"interval": "minmax", "stretch": "linear",
                                                       "colormap": "gray"})
    assert isinstance(r, render.RenderResult)
    assert r.rgb.shape == (60, 90, 3)
    assert r.subtitle
    assert r.composited().shape == r.rgb.shape


def test_render_item_series_uses_slice_index_as_channel():
    ts = TimeSeries(t=np.arange(500, dtype=float), values=np.random.default_rng(0).normal(
        size=(500, 3)), channels=["a", "b", "c"])
    item = DataItem(id="s", modality=Modality.SERIES.value, uri="s.csv", n_views=3,
                    t_ref=0.0, t_end=499.0, meta={"units": ["", "", ""], "discrete": [False] * 3},
                    _opener=lambda: ts)

    r0 = render.render_item(item, size=(200, 100), window=Slice(index=0))
    r2 = render.render_item(item, size=(200, 100), window=Slice(index=2))
    assert r0.rgb.shape == (100, 200, 3)
    assert not np.array_equal(r0.rgb, r2.rgb), "不同通道应画出不同曲线"
    assert r0.info["n_samples"] == 500


def test_render_item_series_reports_axis_bounds():
    ts = TimeSeries(t=np.arange(100, dtype=float), values=(np.arange(100) * 3.0)[:, None],
                    channels=["v"], units=["V"])
    item = DataItem(id="s", modality=Modality.SERIES.value, uri="s.csv",
                    _opener=lambda: ts, meta={"units": ["V"]})
    r = render.render_item(item, size=(300, 120))
    assert r.info["ymin"] < r.info["ymax"]
    assert r.info["plot_box"][0] == render.series.MARGIN_L
    assert "V" in r.subtitle


def test_render_item_event_lanes():
    evs = EventTable([EventRow(t=10.0, label="a", level=0), EventRow(t=50.0, label="b", level=2, t_end=70.0)])
    item = DataItem(id="e", modality=Modality.EVENT.value, uri="e.json", t_ref=10.0, t_end=70.0,
                    _opener=lambda: evs)
    r = render.render_item(item, size=(240, 60))
    assert r.rgb.shape == (60, 240, 3)
    assert "2" in r.subtitle
    assert (r.rgb != 0).any()


def test_render_item_rejects_unknown_modality():
    item = DataItem(id="x", modality="spectrum", uri="x", _opener=lambda: np.zeros((2, 2)))
    with pytest.raises(ValueError):
        render.render_item(item)


def test_overlay_draw_changes_pixels():
    base = np.zeros((40, 60, 3), dtype=np.uint8)
    o = OV.OverlayData()
    o.boxes.append(OV.Box(x0=5, y0=5, x1=30, y1=25, label="src"))
    o.points.append(OV.Point(x=50, y=35))
    out = OV.draw_overlay(base, o)
    assert out.shape == base.shape
    assert out.sum() > 0, "绘制后应出现非零像素"
    assert np.array_equal(base, np.zeros_like(base)), "不得就地修改输入"


def test_overlay_data_extend_and_count():
    a = OV.OverlayData(boxes=[OV.Box(0, 0, 1, 1)], points=[OV.Point(2, 2)])
    b = OV.OverlayData(boxes=[OV.Box(3, 3, 4, 4)])
    assert a.count() == 2
    a.extend(b)
    assert a.count() == 3
    assert OV.OverlayData().is_empty()


def test_render_intervals_respects_window():
    ivs = [OV.VInterval(t0=0.0, t1=10.0, level=0), OV.VInterval(t0=90.0, t1=100.0, level=1)]
    img = OV.render_intervals(100, 20, 0.0, 100.0, ivs)
    assert img.shape == (20, 100, 3)
    left, right = img[:5, :5].sum(), img[15:, 95:].sum()
    assert left > 0 or right > 0


def test_label_placer_rejects_overlap_and_accepts_free_space():
    placer = OV.LabelPlacer(200, 100, cell=8)
    assert placer.try_place(10, 10, 40, 12) is True
    assert placer.try_place(12, 11, 40, 12) is False, "与已放标签重叠应被拒绝"
    assert placer.try_place(120, 60, 40, 12) is True
    assert placer.placed == 2 and placer.skipped == 1


def test_label_placer_clips_out_of_bounds_without_raising():
    placer = OV.LabelPlacer(100, 60, cell=8)
    assert placer.try_place(95, 55, 40, 12) is True, "越界部分应裁进网格而不是报错"
    assert placer.try_place(-5, 55, 40, 12) is True, "跨左边界的标签仍可落在裁出的格子里"
    assert placer.try_place(-50, -50, 20, 12) is False, "完全在网格外放不下"
    assert placer.try_place(1e6, 1e6, 5, 5) is False


def _dense_overlay(n=120, size=240):
    o = OV.OverlayData()
    rng = np.random.default_rng(0)
    for _ in range(n):
        o.points.append(OV.Point(x=float(rng.uniform(4, size - 4)),
                                 y=float(rng.uniform(4, size - 4)), label="1.23e+04"))
    return o


def test_avoid_overlap_reduces_label_pixels_but_keeps_markers():
    base = np.zeros((240, 240, 3), dtype=np.uint8)
    o = _dense_overlay()
    crowded = OV.draw_overlay(base, o, show_labels=True, avoid_overlap=False)
    clean = OV.draw_overlay(base, o, show_labels=True, avoid_overlap=True)
    assert clean.sum() < crowded.sum(), "避让后文字像素应显著减少"

    def has_marker(img, p):
        y, x = int(round(p.y)), int(round(p.x))
        return img[max(0, y - 1):y + 2, max(0, x - 1):x + 2].sum() > 0

    assert all(has_marker(clean, p) for p in o.points), "避让只该丢文字，不能丢标记"
    assert all(has_marker(crowded, p) for p in o.points)


def test_max_labels_budget_is_respected():
    base = np.zeros((300, 300, 3), dtype=np.uint8)
    o = _dense_overlay(n=200, size=290)
    limited = OV.draw_overlay(base, o, avoid_overlap=False, max_labels=5)
    unlimited = OV.draw_overlay(base, o, avoid_overlap=False, max_labels=None)
    assert limited.sum() < unlimited.sum()


def test_show_labels_false_keeps_only_geometry():
    base = np.zeros((120, 120, 3), dtype=np.uint8)
    o = OV.OverlayData(boxes=[OV.Box(10, 10, 60, 40, label="S1")])
    noident = OV.draw_overlay(base, o, show_labels=False)
    assert np.count_nonzero(noident.sum(axis=2)) > 0


def test_mosaic_size_math():
    results = [render.RenderResult(rgb=np.zeros((10, 10, 3), dtype=np.uint8), title=f"t{i}")
               for i in range(4)]
    img = render.render_mosaic(results, rows=2, cols=2, cell=(200, 100), gap=6, pad=4)
    # box = cell_h + label_h(30) = 130; 2*130 + 6 + 2*4 = 274; 2*200 + 6 + 2*4 = 414
    assert img.shape == (274, 414, 3)


def test_mosaic_tolerates_empty_input():
    assert render.render_mosaic([], rows=1, cols=1).size >= 1


def test_mosaic_partial_when_renderer_fails_on_one_item():
    items = [
        DataItem(id="ok", modality=Modality.IMAGE.value, uri="ok", shape=(32, 32),
                 _opener=lambda: _arr(h=32, w=32).astype(float)),
        DataItem(id="bad", modality="bogus", uri="bad", _opener=lambda: None),
    ]
    good, failed = [], []
    for it in items:
        try:
            good.append(render.render_item(it, size=(32, 32), style={}))
        except Exception as exc:
            failed.append((it.id, str(exc)))
    assert len(good) == 1 and len(failed) == 1
    assert render.render_mosaic(good, rows=1, cols=1).shape[2] == 3


# --------------------------------------------------------------------------- #
# 彩色路径
# --------------------------------------------------------------------------- #

def test_render_color_shares_one_interval_across_channels():
    """逐通道各自拉伸会把颜色平衡抹平，病理图上的色差本身就是要比的信息。"""
    h, w = 20, 20
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:, :, 0] = 100
    arr[:, :, 1] = 50
    arr[:, :, 2] = 25
    rgb, info = render.render_color(arr, (h, w))
    mid = rgb[h // 2, w // 2]
    assert info["colormap"] == "native"
    assert [int(v) for v in mid] == [255, 85, 0], "三通道必须按同一组上下界归一"

    per_channel = np.stack([render.stretch.to_rgb(arr[:, :, c], interval="minmax")[0][:, :, 0]
                            for c in range(3)], axis=-1)
    assert np.all(per_channel == 127), \
        "逐通道各自拉伸会把三种不同强度都推成同一个灰值——颜色信息整体丢失"


def test_render_color_keeps_alpha_out_of_output():
    arr = np.zeros((8, 8, 4), dtype=np.uint8)
    arr[:, :, 3] = 128
    rgb, _ = render.render_color(arr, (8, 8))
    assert rgb.shape == (8, 8, 3)


def test_render_array_routes_hwc_to_color_path():
    arr = np.zeros((16, 16, 3), dtype=np.uint16)
    arr[:, :, 2] = 40000
    rgb, info = render.render_array(arr, (16, 16))
    assert info["colormap"] == "native"
    assert np.any(rgb[:, :, 2] > 200) and np.all(rgb[:, :, 0] < 30)


def test_pick_slice_does_not_split_a_single_rgb_image():
    """(h, w, 3) 不是三帧：按第 0 轴切会把一张彩图切成三条通道带。"""
    from mdva.core.model import Slice

    arr = np.zeros((10, 12, 3), dtype=np.uint8)
    arr[:, :, 0] = 11
    arr[:, :, 1] = 22
    arr[:, :, 2] = 33
    item = DataItem(id="rgb", modality=Modality.IMAGE.value, uri="rgb.png",
                    shape=(10, 12), dtype="RGB", n_views=1, _opener=lambda: arr)
    sl = render.pick_slice(item, Slice(index=0))
    assert sl.shape == (10, 12, 3)
    rgb = render.render_item(item, size=(12, 10)).rgb
    assert not np.array_equal(rgb[:, :, 0], rgb[:, :, 1])


def test_rgb_png_roundtrip_keeps_colour(tmp_path):
    from PIL import Image

    from mdva import io

    arr = np.zeros((24, 24, 3), dtype=np.uint8)
    arr[:, :, 0] = 200
    arr[4:20, 4:20, 2] = 240
    path = tmp_path / "c.png"
    Image.fromarray(arr).save(str(path))

    item = io.load_any(str(path))
    assert item.n_views == 1 and item.meta["mode"] == "RGB"
    res = render.render_item(item, size=(24, 24))
    assert res.info["colormap"] == "native"
    assert np.all(res.rgb[:, :, 0] != res.rgb[:, :, 2])


def test_event_views_page_by_severity_level(tmp_path):
    """n_views>1 的每一项都必须真能切出不同画面，否则界面的翻页控件是假的。"""
    import json

    from mdva import io

    path = tmp_path / "ev.json"
    path.write_text(json.dumps({"events": [
        {"time": 100.0, "label": "指令下发", "level": 1},
        {"time": 200.0, "label": "切换备份机", "level": 3},
        {"time": 300.0, "label": "进入安全模式", "level": 3},
    ]}), encoding="utf-8")
    item = io.load_any(str(path))
    assert item.n_views == 3, "全部 + 两个等级"

    whole = render.render_item(item, size=(80, 60))
    assert "3/3" in whole.subtitle and whole.info["level"] is None

    low = render.render_item(item, size=(80, 60), window=Slice(index=1))
    assert low.info["level"] == 1 and "1/3" in low.subtitle

    high = render.render_item(item, size=(80, 60), window=Slice(index=2))
    assert high.info["level"] == 3 and "2/3" in high.subtitle
    assert not np.array_equal(whole.rgb, high.rgb)


def test_event_lane_zooms_with_the_window(tmp_path):
    """放大到某个时刻附近时，事件条必须真的落在画面里，而不是仍然铺满全程。"""
    import json

    from mdva import io

    path = tmp_path / "ev.json"
    path.write_text(json.dumps({"events": [
        {"time": 100.0, "label": "指令", "level": 1},
        {"time": 4000.0, "label": "安全模式", "level": 3},
    ]}), encoding="utf-8")
    item = io.load_any(str(path))

    def bar_columns(sl):
        rgb = render.render_item(item, size=(200, 60), window=sl).rgb
        hit = np.all(rgb == 255, axis=2).any(axis=0)
        return np.flatnonzero(hit)

    wide = bar_columns(Slice.at_time(0.0, 4200.0))
    zoom = bar_columns(Slice.at_time(95.0, 105.0))
    assert wide.size and zoom.size
    assert wide.min() < 20, "全程视图里第一条事件应该贴着左边"
    assert zoom.min() > 60, "放大到 95~105s 时，t=100s 的事件应该画在画面中段"
    assert (zoom > 40).all()


def test_cjk_labels_are_distinguishable():
    """PIL 的内置位图字体没有汉字，标签会全部退化成同一个方框。"""
    from PIL import Image, ImageDraw

    from mdva.core.font import font_path, text_font

    if font_path() is None:
        pytest.skip("本机没有任何 CJK 字体，无法验证中文标签")

    def ink(text):
        img = Image.new("L", (200, 26), 0)
        ImageDraw.Draw(img).text((2, 2), text, fill=255, font=text_font(14))
        return np.asarray(img) > 0

    a, b = ink("窗口内无数据"), ink("动量轮卡死")
    assert a.sum() > 60 and b.sum() > 60
    assert not np.array_equal(a, b), "两组汉字画成了同一个方框"


def test_series_panel_uses_the_font_for_axis_labels():
    """坐标轴刻度改成实测居中，写死偏移会随字号漂移。"""
    from mdva.core.model import TimeSeries

    ts = TimeSeries(t=np.linspace(1735689600.0, 1735689700.0, 200),
                    values=np.random.default_rng(0).normal(0, 1, (200, 1)),
                    channels=["SA_Current"], units=["A"])
    rgb, info = render.render_channel(ts, channel=0, size=(320, 180))
    assert rgb.shape == (180, 320, 3)
    assert info["n_samples"] == 200
    # 底部刻度带必须有墨迹（画不出字时整条都是空的）
    assert (rgb[160:, :, :].max(axis=2) > 60).sum() > 40


def test_spatial_render_ignores_series_only_style_keys(tmp_path):
    """契约 §2.7：23 个帧键是全模态共用参数面。cube 收到时序专属键
    （channel/method/ymin/ymax/out_width）必须忽略而不是 TypeError 500——
    M2 期前端多发一个 channel 就把整幅画布打成了黑帧。"""
    pytest.importorskip("astropy")
    from mdva import io, synth

    path = synth.write_fits(tmp_path / "cube.fits", synth.cube((6, 32, 32), seed=1), {})
    item = io.load_any(str(path))
    assert item.modality == Modality.CUBE.value
    res = render.render_item(item, size=(48, 48), window=Slice(index=2),
                             style={"channel": 3, "method": "minmax", "ymin": 0.0,
                                    "ymax": 1.0, "out_width": 900, "stretch": "sqrt"})
    assert res.rgb.shape[:2] == (48, 48)
    clean = render.render_item(item, size=(48, 48), window=Slice(index=2),
                               style={"stretch": "sqrt"})
    assert np.array_equal(res.rgb, clean.rgb), "被忽略的键不许偷偷影响画面"
