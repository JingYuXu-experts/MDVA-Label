import numpy as np
import pytest

from mdva.core.model import DataItem, EventRow, EventTable, Modality, Slice, TimeSeries


def test_slice_default_is_full_range():
    sl = Slice.full()
    assert not np.isfinite(sl.t0) and not np.isfinite(sl.t1)
    assert sl.contains(0.0) and sl.contains(1e12)


def test_slice_at_time_window():
    sl = Slice.at_time(100.0, 200.0)
    assert sl.contains(100.0) and not sl.contains(200.0)
    assert not sl.contains(99.9)


def test_timeseries_promotes_1d_values_to_single_channel():
    ts = TimeSeries(t=np.arange(5, dtype=float), values=np.arange(5, dtype=float), channels=["a"])
    assert ts.values.shape == (5, 1)
    assert ts.n_channels == 1


def test_timeseries_backfills_channel_names_on_mismatch():
    ts = TimeSeries(t=np.arange(3, dtype=float), values=np.zeros((3, 4)), channels=["x"])
    assert ts.channels == ["ch0", "ch1", "ch2", "ch3"]
    assert ts.units == [""] * 4


def test_timeseries_rejects_length_mismatch():
    with pytest.raises(ValueError):
        TimeSeries(t=np.arange(3, dtype=float), values=np.zeros((5, 2)), channels=["a", "b"])


def test_window_trims_to_bounds():
    ts = TimeSeries(t=np.arange(100, dtype=float), values=np.zeros((100, 2)), channels=["a", "b"])
    sub = ts.window(Slice.at_time(20.0, 30.0))
    assert sub.t[0] == 20.0
    assert sub.t[-1] == 29.0
    assert sub.n_channels == 2


def test_window_on_empty_range_keeps_at_least_one_sample():
    ts = TimeSeries(t=np.arange(10, dtype=float), values=np.arange(10, dtype=float), channels=["a"])
    sub = ts.window(Slice.at_time(50.0, 60.0))
    assert sub.t.size >= 1


def test_event_table_sorts_and_selects():
    table = EventTable([EventRow(t=30.0, label="c"), EventRow(t=10.0, label="a"), EventRow(t=20.0, label="b")])
    assert [r.label for r in table.rows] == ["a", "b", "c"]
    assert [r.label for r in table.select(Slice.at_time(15.0, 25.0)).rows] == ["b"]


def test_event_interval_detected():
    assert EventRow(t=1.0, label="x", t_end=9.0).is_interval
    assert not EventRow(t=1.0, label="x").is_interval


def test_dataitem_load_is_lazy_and_cached():
    calls = {"n": 0}

    def opener():
        calls["n"] += 1
        return np.zeros((4, 4))

    item = DataItem(id="a", modality=Modality.IMAGE.value, uri="a.png", _opener=opener)
    assert calls["n"] == 0 and not item.has_data
    item.load()
    item.load()
    assert calls["n"] == 1 and item.has_data
    item.release()
    item.load()
    assert calls["n"] == 2


def test_dataitem_without_opener_raises():
    with pytest.raises(ValueError):
        DataItem(id="a", modality=Modality.IMAGE.value, uri="a.png").load()


def test_time_span_falls_back_to_t_ref_when_no_end():
    item = DataItem(id="a", modality=Modality.SERIES.value, uri="a.csv", t_ref=100.0)
    assert item.time_span == (100.0, 100.0)
    item.t_end = 200.0
    assert item.time_span == (100.0, 200.0)


def test_overlaps_respects_window():
    item = DataItem(id="a", modality=Modality.SERIES.value, uri="a.csv", t_ref=100.0, t_end=200.0)
    assert item.overlaps(Slice.at_time(150.0, 300.0))
    assert not item.overlaps(Slice.at_time(300.0, 400.0))
