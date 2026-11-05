import numpy as np
import pytest

from mdva.render import downsample as D


def _spiky(n=200_000, seed=0):
    rng = np.random.default_rng(seed)
    v = rng.normal(0.0, 1.0, n)
    idx = np.array([n // 7, n // 3, n // 2, int(0.93 * n)])
    v[idx] += [40.0, -35.0, 60.0, 25.0]
    return v, idx


def test_minmax_preserves_global_extrema_exactly():
    v, _ = _spiky()
    env = D.minmax_envelope(v, 400)
    assert env["max"].max() == pytest.approx(v.max())
    assert env["min"].min() == pytest.approx(v.min())


def test_minmax_keeps_narrow_spikes_that_averaging_would_erase():
    """尖峰宽度远小于分箱宽度时，箱均值会把它抹平，包络不会。"""
    v, idx = _spiky()
    env = D.minmax_envelope(v, 400)
    box = env["box"]
    mids = np.array([v[s:e].mean() for s, e in box])

    for i in idx:
        peak = max(abs(v[i]), 0.0)
        assert max(np.nanmax(env["max"]), np.abs(np.nanmin(env["min"]))) >= peak - 1e-9

    # 尖峰所在箱的均值被严重稀释，包络保留原幅值
    spike_bin = [int(np.searchsorted(box[:, 0], i, side="right") - 1) for i in idx]
    assert np.abs(mids[spike_bin]).max() < np.abs(env["max"][spike_bin]).max()


@pytest.mark.parametrize("out_width", [1, 2, 10, 500, 1000])
def test_envelope_shapes_and_box_coverage(out_width):
    v = np.arange(365, dtype=float)
    env = D.minmax_envelope(v, out_width)
    assert env["x"].shape[0] == min(out_width, v.size)
    assert env["box"].shape[0] == min(out_width, v.size)
    # 每个分箱至少含一个样本，避免 reduceat 空箱行为
    assert np.all(env["box"][:, 1] > env["box"][:, 0]) or env["box"].size == 0


def test_envelope_covers_whole_input_without_gaps():
    v = np.arange(1000, dtype=float)
    env = D.minmax_envelope(v, 37)
    assert env["box"][0, 0] == 0
    assert env["box"][-1, 1] == 1000
    assert np.all(env["box"][1:, 0] == env["box"][:-1, 1])


def test_all_nan_bin_yields_nan_not_fake_value():
    v = np.arange(100, dtype=float)
    v[50:70] = np.nan
    env = D.minmax_envelope(v, 10)
    assert np.any(np.isnan(env["min"])) or np.any(np.isnan(env["max"]))


def test_envelope_uses_time_axis_for_x():
    t = np.linspace(1000.0, 2000.0, 500)
    env = D.minmax_envelope(np.arange(500, dtype=float), 5, t=t)
    assert env["x"][0] == pytest.approx(1000.0)
    assert env["x"].shape == (5,)


def test_envelope_shorter_than_input_returns_input():
    v = np.array([1.0, 2.0, 3.0])
    env = D.minmax_envelope(v, 10)
    assert env["x"].size == 3
    assert env["max"].max() == 3.0


def test_lttb_output_length_and_endpoints():
    t = np.arange(10_000, dtype=float)
    v = np.sin(t / 200.0)
    sx, sy = D.lttb(t, v, 300)
    assert sx.size == 300
    assert sx[0] == t[0] and sx[-1] == t[-1]
    assert np.all(np.diff(sx) > 0)


def test_lttb_no_downsample_when_already_small():
    t = np.arange(10, dtype=float)
    v = np.arange(10, dtype=float)
    sx, sy = D.lttb(t, v, 50)
    assert sx.size == 10 and sy.size == 10


def test_lttb_survives_nan_without_crashing():
    t = np.arange(2000, dtype=float)
    v = np.sin(t / 50.0)
    v[::7] = np.nan
    sx, sy = D.lttb(t, v, 120)
    assert sx.size == 120


def test_lod_pyramid_picks_smallest_sufficient_level():
    t = np.arange(100_000, dtype=float)
    v = np.random.default_rng(2).normal(size=100_000)
    pyr = D.LodPyramid(t, v, base_width=512)
    picked = pyr.level(800)
    assert picked["x"].size <= max(pyr.widths)
    assert 800 <= max(pyr.widths)
    # 同一层级第二次取用必须命中缓存
    key = max(pyr._cache)
    assert pyr.level(800) is pyr._cache[key]


def test_lod_levels_covers_sample_count():
    widths = D.lod_levels(1_000_000, base_width=1024, factor=8)
    assert widths[-1] == 1_000_000
    assert all(a < b for a, b in zip(widths, widths[1:]))


def test_anomaly_retention_is_high_for_envelope_and_low_for_mean():
    v, idx = _spiky(n=50_000)
    env = D.minmax_envelope(v, 300)
    box = env["box"]
    mids = np.array([v[s:e].mean() for s, e in box])

    keep_env = D.anomaly_retention(v, D.envelope_peaks(env), box, idx)
    keep_mean = D.anomaly_retention(v, mids, box, idx)
    assert keep_env == pytest.approx(1.0)
    assert keep_mean < 0.2, "箱均值把尖峰摊薄，保真度必须显著下降"


def test_envelope_peaks_keeps_negative_spikes():
    v = np.zeros(200)
    v[50] = -30.0
    v[150] = 12.0
    env = D.minmax_envelope(v, 4)
    peaks = D.envelope_peaks(env)
    assert peaks.min() == pytest.approx(-30.0)
    assert peaks.max() == pytest.approx(12.0)
    assert D.anomaly_retention(v, peaks, env["box"], [50, 150]) == pytest.approx(1.0)


def test_detect_anomaly_indices_finds_injected_spikes():
    v, idx = _spiky(n=20_000, seed=5)
    found = D.detect_anomaly_indices(v, nsigma=8.0)
    assert found.size >= idx.size
    assert set(idx).issubset(set(found.tolist()))


def test_empty_input_returns_empty_envelope():
    env = D.minmax_envelope(np.array([]), 10)
    assert env["x"].size == 0 and env["min"].size == 0
