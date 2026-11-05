import json

import numpy as np
import pytest

from mdva import io
from mdva.core.model import DataItem, Modality, Slice, TimeSeries
from mdva.io import image as imgio
from mdva.io import series as sio
from mdva.io.timeutil import to_utc_seconds


@pytest.mark.parametrize("text,expected", [
    ("2025-01-01T00:00:00.000", 1735689600.0),
    ("2025-01-01T00:00:00Z", 1735689600.0),
    ("2025-01-01 12:30:00", 1735734600.0),
    ("20250101.000000", 1735689600.0),
    ("2025-01-01", 1735689600.0),
])
def test_timeutil_parses_common_fits_formats(text, expected):
    assert to_utc_seconds(text) == pytest.approx(expected)


def test_timeutil_rejects_garbage_and_passes_numbers():
    assert to_utc_seconds("not a date") is None
    assert to_utc_seconds(None) is None
    assert to_utc_seconds("") is None
    assert to_utc_seconds(1735689600) == pytest.approx(1735689600.0)


@pytest.mark.parametrize("text,expected", [
    ("1735689600", 1735689600.0),      # 同一列里 60 秒的两种写法必须给出同一种解释
    ("1735689660", 1735689660.0),
    ("1735689630.500", 1735689630.5),
])
def test_ten_digit_epoch_string_is_not_read_as_a_year_1735_date(text, expected):
    assert to_utc_seconds(text) == pytest.approx(expected)
    assert to_utc_seconds(text) > 1e9      # 掉进 1735 年会是负数


def test_compact_digit_dates_still_parse():
    assert to_utc_seconds("20250101") == pytest.approx(1735689600.0)
    assert to_utc_seconds("20250101000000") == pytest.approx(1735689600.0)
    assert to_utc_seconds("2025010") is None      # 位数不够的残缺紧凑日期不猜


def test_fits_mjd_conversion_matches_epoch():
    # MJD 40587.0 == 1970-01-01T00:00:00UT，即 Unix 纪元
    assert imgio.fits_mjd_to_utc(40587.0) == pytest.approx(0.0)
    assert imgio.fits_mjd_to_utc(40588.0) == pytest.approx(86400.0)
    assert imgio.fits_mjd_to_utc(60000.0) == pytest.approx((60000.0 - 40587.0) * 86400.0)
    assert imgio.fits_mjd_to_utc(None) is None
    assert imgio.fits_mjd_to_utc(float("nan")) is None


def test_fits_time_prefers_mjd_over_string():
    header = {"MJD-OBS": 40589.0, "DATE-OBS": "2020-01-01T00:00:00"}
    assert imgio.fits_time_to_utc(header) == pytest.approx(2 * 86400.0)


def test_fits_time_ignores_relative_tstart():
    """TSTART 常是相对秒，误当绝对时间会把数据甩到 1970 年。"""
    assert imgio.fits_time_to_utc({"TSTART": 1234.5, "DATE-OBS": "2025-06-01T00:00:00"}) is not None
    assert imgio.fits_time_to_utc({"TSTART": 1234.5}) is None


def test_fits_roundtrip_preserves_data_and_time(tmp_path):
    pytest.importorskip("astropy")
    from astropy.io import fits

    arr = np.arange(24 * 32, dtype=np.uint16).reshape(24, 32)
    path = tmp_path / "a.fits"
    hdu = fits.PrimaryHDU(arr)
    hdu.header["DATE-OBS"] = "2025-03-04T05:06:07.000"
    hdu.header["BUNIT"] = "counts"
    hdu.header["TELESCOP"] = "TEST-SAT"
    hdu.header["OBJECT"] = "CRAB"
    hdu.writeto(str(path), overwrite=True)

    item = imgio.load_fits(str(path))
    assert item.shape == (24, 32)
    assert item.modality == Modality.IMAGE.value
    assert item.n_views == 1
    assert item.t_ref == pytest.approx(to_utc_seconds("2025-03-04T05:06:07"))
    assert item.meta["unit"] == "counts"
    assert item.meta["telescope"] == "TEST-SAT"
    assert item.title == "CRAB"
    assert np.array_equal(item.load(), arr)


def test_fits_cube_reports_slice_count(tmp_path):
    pytest.importorskip("astropy")
    from astropy.io import fits

    arr = np.random.default_rng(0).integers(0, 65535, (8, 16, 16)).astype(np.uint16)
    path = tmp_path / "cube.fits"
    fits.PrimaryHDU(arr).writeto(str(path), overwrite=True)

    item = imgio.load_fits(str(path))
    assert item.modality == Modality.CUBE.value
    assert item.n_views == 8
    assert imgio.slice_at(item, 3).shape == (16, 16)
    assert imgio.slice_at(item, 99).shape == (16, 16)  # 越界夹到末片而不是报错


def test_fits_skips_empty_primary_and_picks_first_image_hdu(tmp_path):
    pytest.importorskip("astropy")
    from astropy.io import fits

    primary = fits.PrimaryHDU()  # 无数据的头
    sci = fits.ImageHDU(np.ones((10, 10), dtype=np.float32), name="SCI")
    path = tmp_path / "mef.fits"
    fits.HDUList([primary, sci]).writeto(str(path), overwrite=True)

    item = imgio.load_fits(str(path))
    assert item.meta["hdu"] == 1
    assert item.load().shape == (10, 10)


def test_png_load_reports_shape_and_defers_pixels(tmp_path):
    from PIL import Image

    path = tmp_path / "p.png"
    Image.fromarray(np.zeros((7, 9, 3), dtype=np.uint8)).save(str(path))

    item = imgio.load_image(str(path))
    assert item.shape == (7, 9)
    assert item.modality == Modality.IMAGE.value
    assert not item.has_data
    assert item.load().shape == (7, 9, 3)


def test_load_dispatches_by_suffix(tmp_path):
    from PIL import Image

    path = tmp_path / "x.jpg"
    Image.fromarray(np.zeros((4, 4, 3), dtype=np.uint8)).save(str(path))
    assert imgio.load(str(path)).modality == Modality.IMAGE.value

    with pytest.raises(ValueError):
        imgio.load(str(tmp_path / "x.raw"))


def test_series_loads_time_column_and_channels(tmp_path):
    t0 = 1735689600.0
    path = tmp_path / "t.csv"
    lines = ["time,bus_a,temp_b"]
    lines += [f"{t0 + i},{28.0 + i * 0.01},{20.0 + (i % 5)}" for i in range(50)]
    path.write_text("\n".join(lines), encoding="utf-8")

    item = sio.load_series(str(path))
    assert item.modality == Modality.SERIES.value
    ts = item.load()
    assert ts.channels == ["bus_a", "temp_b"]
    assert ts.t.size == 50
    assert ts.t[0] == pytest.approx(t0)
    assert item.t_end == pytest.approx(ts.t[-1])
    assert item.meta["median_step"] == pytest.approx(1.0)


def test_series_applies_scale_and_offset_calibration(tmp_path):
    path = tmp_path / "c.csv"
    path.write_text("utc,raw_v\n" + "\n".join(f"{1000 + i},{i}" for i in range(10)), encoding="utf-8")

    calib = sio.Calibration({"raw_v": {"type": "analog", "scale": 0.5, "offset": 3.0, "unit": "V"}})
    ts = sio.load_series(str(path), calibration=calib).load()
    assert ts.values[:, 0][0] == pytest.approx(3.0)
    assert ts.values[:, 0][5] == pytest.approx(5.5)
    assert ts.units == ["V"]


def test_calibration_marks_discrete_channels(tmp_path):
    path = tmp_path / "d.csv"
    path.write_text("utc,mode\n" + "\n".join(f"{i},{i % 3}" for i in range(9)), encoding="utf-8")

    calib = sio.Calibration({
        "mode": {"type": "discrete", "values": {"0": "Standby", "1": "Science", "2": "Safe"}}
    })
    ts = sio.load_series(str(path), calibration=calib).load()
    assert ts.is_discrete(0) is True
    assert calib.labels("mode")[1] == "Science"


def test_calibration_csv_form(tmp_path):
    table = tmp_path / "cal.csv"
    # values 用分号形式，避免 JSON 里的逗号破坏 CSV 分列
    table.write_text(
        'channel,type,scale,offset,unit,values\n'
        'v_analog,analog,0.001,10.0,V,\n'
        's_state,discrete,,,,0:Off;1:On\n',
        encoding="utf-8",
    )
    calib = sio.Calibration.load(str(table))
    assert calib.is_discrete("s_state")
    assert not calib.is_discrete("v_analog")
    assert calib.apply("v_analog", np.array([1000.0]))[0] == pytest.approx(11.0)
    assert calib.labels("s_state")[1] == "On"
    assert calib.unit("v_analog") == "V"


def test_series_without_time_column_uses_start_and_dt(tmp_path):
    path = tmp_path / "n.csv"
    path.write_text("a\n" + "\n".join(str(i * 1.0) for i in range(20)), encoding="utf-8")

    item = sio.load_series(str(path), start=500.0, dt=2.0)
    ts = item.load()
    assert ts.t[0] == pytest.approx(500.0)
    assert ts.t[3] == pytest.approx(506.0)


def test_relative_time_column_gets_base_time(tmp_path):
    path = tmp_path / "met.csv"
    path.write_text("met,v\n" + "\n".join(f"{i * 10},{i}" for i in range(6)), encoding="utf-8")

    ts = sio.load_series(str(path), start=1735689600.0).load()
    assert ts.t[0] == pytest.approx(1735689600.0)
    assert ts.t[2] == pytest.approx(1735689620.0)


def test_series_sorts_unordered_input(tmp_path):
    path = tmp_path / "u.csv"
    path.write_text("utc,v\n300,3\n100,1\n200,2", encoding="utf-8")
    ts = sio.load_series(str(path)).load()
    assert list(ts.t) == [100.0, 200.0, 300.0]
    assert list(ts.values[:, 0]) == [1.0, 2.0, 3.0]


def test_resample_mean_aggregates_and_nearest_keeps_discrete():
    ts = TimeSeries(
        t=np.arange(0.0, 10.0, 1.0),
        values=np.stack([np.arange(10.0), np.zeros(10) + 5], axis=1),
        channels=["analog", "state"],
        discrete=[False, True],
    )
    out = sio.resample(ts, 2.0, agg="mean")
    assert out.t.size == 5
    assert out.values[1, 0] == pytest.approx(np.mean([2.0, 3.0]))
    assert out.is_discrete(1)
    # 离散通道保持原值，不被平均成不存在的状态
    assert set(out.values[:, 1]) == {5.0}


def test_resample_max_min_and_gap_masking():
    ts = TimeSeries(t=np.arange(0.0, 12.0), values=np.arange(12.0)[:, None], channels=["a"])
    assert sio.resample(ts, 3.0, agg="max").values[:, 0].tolist() == [2.0, 5.0, 8.0, 11.0]
    assert sio.resample(ts, 3.0, agg="min").values[:, 0].tolist() == [0.0, 3.0, 6.0, 9.0]

    gapped = np.arange(12.0)
    gapped[4:9] = np.nan
    ts2 = TimeSeries(t=np.arange(0.0, 12.0), values=gapped[:, None], channels=["a"])
    out = sio.resample(ts2, 1.0, agg="mean", max_gap=2)
    assert np.isnan(out.values[4:9, 0]).all()
    assert not np.isnan(out.values[2, 0])


def test_event_loads_json_with_levels_and_duration(tmp_path):
    from mdva.io import event

    path = tmp_path / "ev.json"
    path.write_text(json.dumps({"events": [
        {"time": "2025-01-01T00:10:00", "label": "cmd upload", "level": "info", "source": "S-band"},
        {"time": "2025-01-01T00:20:00", "label": "heater fault", "severity": "warning",
         "subsys": "thermal", "duration": 300},
        {"time": 1735690800, "message": "safe mode", "level": 4},
    ]}), encoding="utf-8")

    item = event.load_events(str(path))
    evs = item.load()
    assert item.modality == Modality.EVENT.value
    assert len(evs.rows) == 3
    assert [r.level for r in evs.rows] == sorted([r.level for r in evs.rows])
    fault = [r for r in evs.rows if r.label == "heater fault"][0]
    assert fault.is_interval and fault.t_end - fault.t == pytest.approx(300.0)
    assert fault.source == "thermal"
    assert item.meta["n_events"] == 3


def test_event_numeric_offsets_are_seconds_relative_to_start(tmp_path):
    from mdva.io import event

    path = tmp_path / "e.json"
    path.write_text(json.dumps([{"t": 10, "label": "a"}, {"t": 20, "label": "b"}]), encoding="utf-8")
    evs = event.load_events(str(path), start=1000.0).load()
    assert [r.t for r in evs.rows] == [1010.0, 1020.0]


def test_event_unparsable_strings_fall_back_to_frame_index(tmp_path):
    from mdva.io import event

    path = tmp_path / "f.json"
    path.write_text(json.dumps([{"time": "7", "label": "a"}]), encoding="utf-8")
    evs = event.load_events(str(path), start=1000.0, dt=0.5).load()
    assert [r.t for r in evs.rows] == [1003.5]


def test_event_plain_log(tmp_path):
    from mdva.io import event

    path = tmp_path / "log.txt"
    path.write_text(
        "2025-01-01T00:00:10 [WARNING] temp above limit\n"
        "2025-01-01T00:00:40 [INFO] nominal resumed\n",
        encoding="utf-8",
    )
    evs = event.load_events(str(path)).load()
    assert len(evs.rows) == 2
    assert evs.rows[0].level == 2


def test_scan_filters_unknown_and_lists_all_modalities(tmp_path):
    pytest.importorskip("astropy")
    from mdva import synth

    root = synth.make_dataset(tmp_path / "ds", n_telemetry=2000, seed=3)
    (root / "notes.md").write_text("ignored", encoding="utf-8")

    items = io.scan(root)
    kinds = sorted({i.modality for i in items})
    assert Modality.SERIES.value in kinds
    assert Modality.EVENT.value in kinds
    assert all(i.modality != "md" for i in items)
    assert not any("notes" in i.id for i in items)
    assert not any(i.has_data for i in items), "scan 只探测头，不应把数据读进内存"


def test_common_span_and_window_filter(tmp_path):
    pytest.importorskip("astropy")
    from mdva import synth

    items = io.scan(synth.make_dataset(tmp_path / "ds2", n_telemetry=1000, seed=1))
    t0, t1 = io.common_span(items)
    assert t1 > t0
    inside = io.items_in_window(items, Slice.at_time(t0, t0 + 1.0))
    assert inside
    assert all(i.overlaps(Slice.at_time(t0, t0 + 1.0)) for i in inside)


def test_sniff_resolves_ambiguous_csv(tmp_path):
    tele = tmp_path / "a.csv"
    tele.write_text("utc,v\n1000,1.0\n1001,2.0", encoding="utf-8")
    assert io.sniff(str(tele)) == Modality.SERIES.value

    unknown = tmp_path / "z.xyz"
    unknown.write_text("x", encoding="utf-8")
    assert io.modality_of(str(unknown)) is None


def test_sniff_refuses_to_invent_a_time_axis(tmp_path):
    """指标报表不是时序：没有时间列时只有纯数值表才配用采样序号补时间。"""
    metrics = tmp_path / "metrics.csv"
    metrics.write_text("scene,model,iou_blob\nscene_000,unet,0.81\nscene_001,unet,0.77",
                       encoding="utf-8")
    assert io.sniff(str(metrics)) == ""
    assert io.scan(tmp_path) == []

    headerless = tmp_path / "values.csv"
    headerless.write_text("1.0\n2.0\n3.0", encoding="utf-8")
    assert io.sniff(str(headerless)) == Modality.SERIES.value


def test_sniff_rejects_unknown_suffix(tmp_path):
    notes = tmp_path / "readme.md"
    notes.write_text("# 说明\n utc, v\n 1, 2\n", encoding="utf-8")
    assert io.sniff(str(notes)) == ""


def test_series_sidecar_names_are_not_data_items(tmp_path):
    (tmp_path / "calib.json").write_text('{"a": {"type": "analog"}}', encoding="utf-8")
    (tmp_path / "meta.json").write_text('{"fps": 1}', encoding="utf-8")
    (tmp_path / "truth.json").write_text('{"x": 1}', encoding="utf-8")
    assert io.scan(tmp_path) == []


def test_calibration_sidecar_is_picked_up_automatically(tmp_path):
    """离散通道少了标定表就会被当连续量插值，产出根本不存在的中间状态。"""
    table = tmp_path / "eng.csv"
    table.write_text("utc,mode\n0,0\n1,1\n2,2", encoding="utf-8")
    (tmp_path / "eng.calib.json").write_text(
        json.dumps({"mode": {"type": "discrete", "values": {0: "Standby", 1: "Science"}}}),
        encoding="utf-8")

    item = io.load_any(str(table))
    assert item.meta["discrete"] == [True]

    # 显式传 calibration 时不应被自动发现覆盖
    other = tmp_path / "other.calib.json"
    other.write_text(json.dumps({"mode": {"type": "analog"}}), encoding="utf-8")
    from mdva.io.series import Calibration

    forced = io.load_any(str(table), calibration=Calibration.load(other))
    assert forced.meta["discrete"] == [False]


def test_common_span_ignores_untimed_items():
    """一个没有绝对时刻的项不能把整条时间轴撑成无限宽。"""
    timed = DataItem(id="tele.csv", modality=Modality.SERIES.value, uri="tele.csv",
                     t_ref=100.0, t_end=200.0)
    untimed = DataItem(id="ct.npy", modality=Modality.VIDEO.value, uri="ct.npy")
    assert untimed.t_ref is None and untimed.time_span[0] == float("-inf")
    assert io.common_span([timed, untimed]) == (100.0, 200.0)
    assert io.common_span([untimed]) == (float("-inf"), float("inf"))
