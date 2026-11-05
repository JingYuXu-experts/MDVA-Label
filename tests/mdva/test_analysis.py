import numpy as np
import pytest

from mdva import analysis
from mdva.core.model import DataItem, Modality, Slice, TimeSeries
from mdva.synth import star_field


def _image_item(arr, uri="synth.fits"):
    return DataItem(
        id=uri, modality=Modality.IMAGE.value, uri=uri,
        shape=arr.shape, dtype=str(arr.dtype),
        _opener=lambda: np.asarray(arr, dtype=np.float64),
    )


def _series_item(channels, values, t=None, discrete=None):
    n = values.shape[0]
    tt = np.arange(n, dtype=float) * 1.0 if t is None else t
    ts = TimeSeries(t=tt, values=values, channels=list(channels),
                    units=[""] * len(channels),
                    discrete=list(discrete) if discrete else [False] * len(channels))
    return DataItem(
        id="tele.csv", modality=Modality.SERIES.value, uri="tele.csv",
        t_ref=tt[0], t_end=tt[-1], meta={"discrete": ts.discrete},
        _opener=lambda: ts,
    )


def test_registry_exposes_all_builtin_analyzers():
    for name in ("bg_stats", "star_detect", "cosmic_reject", "image_quality",
                 "rolling_stats", "dynamic_threshold", "changepoint", "correlation", "iforest"):
        assert name in analysis.names()
    assert analysis.get("star_detect").name == "star_detect"


def test_unknown_analyzer_raises_with_available_names():
    from mdva.core.registry import ANALYZERS

    with pytest.raises(KeyError) as exc:
        ANALYZERS.get("does_not_exist")
    assert "star_detect" in str(exc.value), "报错应列出可用算子"


def test_analyzer_defaults_come_from_schema():
    inst = analysis.get("dynamic_threshold")
    assert inst.params["window"] == 300
    assert inst.params["nsigma"] == 5.0
    overridden = analysis.get("dynamic_threshold", nsigma=9.0)
    assert overridden.params["nsigma"] == 9.0


def test_bg_stats_recovers_injected_background():
    arr, _ = star_field(shape=(256, 256), n_sources=20, bg=120.0, noise_sigma=6.0, seed=1)
    res = analysis.get("bg_stats").run(_image_item(arr))
    assert abs(res.metrics["bg_median"] - 120.0) < 3.0
    assert abs(res.metrics["bg_sigma"] - 6.0) < 2.0
    assert res.metrics["n_valid"] == 256 * 256


def test_star_detect_recovers_most_sources_with_subpixel_centroids():
    arr, truth = star_field(shape=(512, 512), n_sources=60, seed=2)
    res = analysis.get("star_detect", nsigma=5.0).run(_image_item(arr))
    found = res.artifacts["centroids"]

    assert len(found) >= 45, "暗弱源允许漏检，但完整率不应低于 75%"

    errs = []
    for cx, cy, flux in truth["centroids"]:
        best = min(found, key=lambda f: (f[0] - cx) ** 2 + (f[1] - cy) ** 2)
        d = np.hypot(best[0] - cx, best[1] - cy)
        if d < 2.0:
            errs.append((flux, d))
    assert len(errs) >= 40

    bright = sorted(errs, key=lambda e: -e[0])[:30]
    median_err = float(np.median([d for _, d in bright]))
    assert median_err < 0.35, f"亮源亚像元质心误差过大: {median_err}"


def test_star_detect_returns_empty_result_on_blank_frame():
    arr = np.full((64, 64), 100.0) + np.random.default_rng(0).normal(0, 1.0, (64, 64))
    res = analysis.get("star_detect").run(_image_item(arr))
    assert res.metrics["n_sources"] == 0
    assert res.overlay.is_empty()


def test_star_detect_marks_boxes_and_points_per_source():
    arr, _ = star_field(shape=(256, 256), n_sources=15, seed=4)
    res = analysis.get("star_detect").run(_image_item(arr))
    n = res.metrics["n_sources"]
    assert len(res.overlay.points) == n == len(res.overlay.boxes)


def test_cosmic_reject_finds_injected_single_pixel_spikes():
    arr, truth = star_field(shape=(256, 256), n_sources=10, n_cosmic=50, seed=6)
    res = analysis.get("cosmic_reject", nsigma=6.0).run(_image_item(arr))
    mask = res.artifacts["mask"]
    hits = sum(1 for x, y in truth["cosmic"] if mask[y, x])
    assert hits >= 25, f"宇宙线检出过少: {hits}/50"
    # 不应把高斯点源误判为宇宙线
    false_on_stars = sum(1 for cx, cy, _ in truth["centroids"]
                         if mask[int(round(cy)), int(round(cx))])
    assert false_on_stars <= 2


def test_image_quality_sees_blurring_reduce_high_frequency_energy():
    arr, _ = star_field(shape=(256, 256), n_sources=40, seed=7)
    from scipy.ndimage import gaussian_filter

    sharp = analysis.get("image_quality").run(_image_item(arr))
    blur = analysis.get("image_quality").run(_image_item(gaussian_filter(arr.astype(float), 2.0)))
    assert sharp.metrics["hf_ratio"] > blur.metrics["hf_ratio"]
    assert sharp.metrics["sharpness"] > blur.metrics["sharpness"]
    assert sharp.metrics["bad_fraction"] == 0.0


def test_image_quality_counts_bad_pixels():
    arr = np.ones((50, 50)) * 5.0
    arr[:5, :5] = np.nan
    res = analysis.get("image_quality").run(_image_item(arr))
    assert res.metrics["bad_fraction"] == pytest.approx(25 / 2500)


def test_dynamic_threshold_flags_sharp_excursions_in_quiet_channel():
    rng = np.random.default_rng(0)
    n = 4000
    v = rng.normal(28.0, 0.02, n)
    v[1000:1010] += 3.0
    v[2500:2506] -= 2.5
    item = _series_item(["bus_v"], v[:, None])

    res = analysis.get("dynamic_threshold", window=200, nsigma=5.0).run(item)
    assert res.metrics["n_events"] >= 2
    assert 0 < res.metrics["excursion_fraction"] < 0.05
    spans = [(f.box[0], f.box[1]) for f in res.flags]
    assert any(abs(s - 1000) <= 5 for s, e in spans)


def test_dynamic_threshold_adapts_to_slow_periodic_baseline():
    """基线缓慢起伏时不应误报。静态门限在这种场景会整段报警。"""
    rng = np.random.default_rng(2)
    t = np.arange(4000, dtype=float)
    v = 5.0 + 3.0 * np.sin(2 * np.pi * t / 1200.0) + rng.normal(0, 0.02, t.size)
    res = analysis.get("dynamic_threshold", window=200, nsigma=5.0).run(_series_item(["p"], v[:, None]))
    assert res.metrics["excursion_fraction"] < 0.02


def test_dynamic_threshold_adapts_to_level_shift():
    """基线抬升后仍应正常工作，这正是静态门限失效的场景。"""
    rng = np.random.default_rng(1)
    low = rng.normal(20.0, 0.3, 2000)
    high = rng.normal(200.0, 0.3, 2000)
    v = np.concatenate([low, high])
    v[-40:] += 8.0  # 在新基线上的一次越界
    res = analysis.get("dynamic_threshold", window=200, nsigma=5.0).run(_series_item(["s"], v[:, None]))
    assert any(f.box and f.box[0] > 3900 for f in res.flags)


def test_changepoint_locates_injected_mean_step():
    rng = np.random.default_rng(3)
    v = np.concatenate([rng.normal(0, 1, 1500), rng.normal(6, 1, 1500)])
    res = analysis.get("changepoint", threshold=8.0).run(_series_item(["c"], v[:, None]))
    assert res.metrics["n_changepoints"] >= 1
    times = [t for t, _ in res.artifacts["changepoints"]]
    assert min(abs(t - 1500.0) for t in times) < 60.0


def test_changepoint_on_noise_is_conservative():
    v = np.random.default_rng(4).normal(0, 1, 2000)
    res = analysis.get("changepoint", threshold=12.0).run(_series_item(["n"], v[:, None]))
    assert res.metrics["n_changepoints"] == 0


def test_correlation_finds_the_coupled_pair():
    rng = np.random.default_rng(5)
    n = 3000
    driver = rng.normal(0, 1, n)
    a = driver * 2 + rng.normal(0, 0.05, n)
    b = -driver + rng.normal(0, 0.05, n)
    c = rng.normal(0, 1, n)
    res = analysis.get("correlation").run(_series_item(["a", "b", "c"], np.stack([a, b, c], axis=1)))
    pairs = res.artifacts["pairs"]
    assert pairs
    top = pairs[0]
    assert {top[0], top[1]} == {"a", "b"}
    assert top[2] < -0.95
    assert res.metrics["n_channels"] == 3


def test_iforest_flags_injected_outliers_at_configured_rate():
    rng = np.random.default_rng(6)
    n = 3000
    x = rng.normal(0, 1, (n, 3))
    x[500:520] += 8.0
    res = analysis.get("iforest", contamination=0.02).run(_series_item(["a", "b", "c"], x))
    assert res.metrics["n_anomalies"] > 0
    assert abs(res.metrics["rate"] - 0.02) < 0.05
    times = set(np.round(res.artifacts["anomaly_time"]).astype(int))
    assert len(times & set(range(500, 520))) >= 8


def test_iforest_rejects_too_short_input():
    v = np.random.default_rng(7).normal(size=(5, 2))
    res = analysis.get("iforest").run(_series_item(["a", "b"], v))
    assert res.metrics["usable"] is False


def test_rolling_stats_tracks_window_mean():
    v = np.arange(200.0)
    res = analysis.get("rolling_stats", window=20).run(_series_item(["r"], v[:, None]))
    assert res.metrics["local_min"] < res.metrics["local_max"]
    assert res.artifacts["rolling_mean"].size == 200


def test_run_all_merges_overlays_and_prefixes_metrics():
    arr, _ = star_field(shape=(256, 256), n_sources=12, seed=8)
    item = _image_item(arr)
    merged = analysis.run_all(item, ["bg_stats", "star_detect", "image_quality"])
    assert "bg_stats.bg_median" in merged.metrics
    assert "star_detect.n_sources" in merged.metrics
    assert "image_quality.hf_ratio" in merged.metrics
    assert merged.overlay.count() > 0
    assert merged.analyzer == "bg_stats,star_detect,image_quality"


def test_analyzer_modality_guard():
    arr, _ = star_field(shape=(64, 64), n_sources=3, seed=9)
    assert analysis.get("star_detect").handles(_image_item(arr))
    assert not analysis.get("star_detect").handles(_series_item(["a"], np.zeros((10, 1))))
    assert not analysis.get("dynamic_threshold").handles(_image_item(arr))


def test_analyze_result_summary_is_readable():
    arr, _ = star_field(shape=(128, 128), n_sources=8, seed=10)
    res = analysis.get("star_detect").run(_image_item(arr))
    text = res.summary()
    assert text.startswith("[star_detect]")
    assert "n_sources=" in text


INT_DTYPES = ["uint16", "uint32", "int16", "int32", "float32", "float64"]


@pytest.mark.parametrize("dtype", INT_DTYPES)
@pytest.mark.parametrize("analyzer", ["bg_stats", "star_detect", "cosmic_reject", "image_quality"])
def test_image_analyzers_are_dtype_safe(analyzer, dtype):
    """FITS 常见整数位深不得让算子静默算错。

    uint16 相减会把负残差回绕成大正数，不报错只给错结果，
    必须用真实位深跑一遍才算覆盖。
    """
    arr, truth = star_field(shape=(256, 256), n_sources=12, n_cosmic=25, seed=12)
    item = _image_item(arr.astype(dtype))
    res = analysis.get(analyzer).run(item)
    assert res.metrics, f"{analyzer}/{dtype} 没有产出任何指标"
    assert all(np.isfinite(v) for v in res.metrics.values()
               if isinstance(v, float)), f"{analyzer}/{dtype} 指标出现非有限值"


@pytest.mark.parametrize("dtype", INT_DTYPES)
def test_cosmic_reject_detects_equally_across_dtypes(dtype):
    """回归用例：uint16 输入曾因无符号回绕导致检出数从 25 掉到 0。"""
    arr, truth = star_field(shape=(256, 256), n_sources=12, n_cosmic=25, seed=13)
    res = analysis.get("cosmic_reject").run(_image_item(arr.astype(dtype)))
    hits = sum(1 for x, y in truth["cosmic"] if res.artifacts["mask"][y, x])
    assert hits >= 15, f"{dtype} 下宇宙线检出仅 {hits}/25，疑似整数回绕"
    assert res.metrics["resid_sigma"] < 100, f"{dtype} 残差 sigma 异常: {res.metrics['resid_sigma']}"


@pytest.mark.parametrize("dtype", INT_DTYPES)
def test_star_detect_centroids_stable_across_dtypes(dtype):
    arr, truth = star_field(shape=(256, 256), n_sources=20, seed=14)
    res = analysis.get("star_detect").run(_image_item(arr.astype(dtype)))
    found = res.artifacts["centroids"]
    errs = []
    for cx, cy, _ in truth["centroids"]:
        best = min(found, key=lambda f: (f[0] - cx) ** 2 + (f[1] - cy) ** 2)
        d = np.hypot(best[0] - cx, best[1] - cy)
        if d < 2.0:
            errs.append(d)
    assert len(errs) >= 12
    assert float(np.median(errs)) < 0.5, f"{dtype} 质心误差过大"


def test_real_fits_uint16_path_detects_cosmics(tmp_path):
    """端到端：走真实 FITS 加载（返回 uint16）而不是测试里手工转的 float。"""
    pytest.importorskip("astropy")
    from mdva import io
    from mdva.synth import write_fits

    arr, truth = star_field(shape=(512, 512), n_sources=60, n_cosmic=40, seed=7)
    path = write_fits(tmp_path / "c.fits", arr, {"TELESCOP": "SIM", "DATE-OBS": "2025-01-01T00:00:00.000"})
    item = io.load_any(str(path))
    assert item.load().dtype == np.uint16, "加载器应保留原生位深"

    res = analysis.get("cosmic_reject").run(item)
    hits = sum(1 for x, y in truth["cosmic"] if res.artifacts["mask"][y, x])
    assert hits >= 30, f"真实 FITS 路径下仅检出 {hits}/40"
    assert res.metrics["resid_sigma"] < 100


def test_to_float_guards_unsigned_subtraction():
    from mdva.analysis.base import to_float

    a = np.array([10, 5], dtype=np.uint16)
    assert (a - np.uint16(8)).dtype == np.uint16
    assert (a - np.uint16(8))[1] == 65533, "确认无符号回绕真实存在"
    f = to_float(a)
    assert f.dtype == np.float64
    assert (f - 8.0)[1] == -3.0


def test_window_restricts_series_analysis():
    v = np.zeros((1000, 1))
    v[100:110] = 50.0
    item = _series_item(["a"], v)
    full = analysis.get("dynamic_threshold", window=100, nsigma=4.0).run(item)
    clipped = analysis.get("dynamic_threshold", window=100, nsigma=4.0).run(
        item, Slice.at_time(700.0, 999.0))
    assert full.metrics["n_events"] >= 1
    assert clipped.metrics["n_events"] == 0


# --------------------------------------------------------------------------- #
# 彩色输入
# --------------------------------------------------------------------------- #

def test_to_gray_matches_bt601_luma():
    from mdva.analysis.base import to_gray

    def luma(channel):
        arr = np.zeros((4, 5, 3), dtype=np.uint8)
        arr[..., channel] = 255
        out = to_gray(arr)
        assert out.shape == (4, 5) and out.dtype == np.float64
        return float(out[0, 0])

    r, g, b = luma(0), luma(1), luma(2)
    assert (r, g, b) == pytest.approx((76.245, 149.685, 29.07))
    assert g > r > b, "感知亮度必须绿>红>蓝，三通道平均会得到 r==g==b"


def test_to_gray_drops_alpha_and_leaves_2d_alone():
    from mdva.analysis.base import to_float, to_gray

    rgba = np.ones((6, 6, 4), dtype=np.uint16)
    rgba[..., 3] = 7
    assert np.allclose(to_gray(rgba), to_float(np.ones((6, 6))))

    flat = np.arange(12, dtype=np.uint16).reshape(3, 4)
    assert np.array_equal(to_gray(flat), to_float(flat))


@pytest.mark.parametrize("name", ["bg_stats", "star_detect", "cosmic_reject", "image_quality"])
def test_image_analyzers_accept_rgb(name):
    """彩色图曾让 `h, w = arr.shape` 直接崩掉，四个图像算子都得能吃完。"""
    rng = np.random.default_rng(0)
    rgb = np.dstack([star_field(shape=(64, 64), n_sources=8, seed=1)[0]] * 3).astype(np.uint8)
    rgb = (rgb * rng.uniform(0.6, 1.0, (1, 1, 3))).astype(np.uint8)
    item = DataItem(id="c.png", modality=Modality.IMAGE.value, uri="c.png",
                    shape=rgb.shape[:2], dtype="RGB", n_views=1, _opener=lambda: rgb)
    res = analysis.get(name).run(item, Slice.full())
    assert res.metrics
    assert all(np.isfinite(v) for v in res.metrics.values() if isinstance(v, (int, float)))


def test_image_analyzers_handle_video_frames():
    from mdva.core.model import Modality as M

    item = DataItem(id="seq", modality=M.VIDEO.value, uri="seq", n_views=4,
                    shape=(4, 32, 32), dtype="frame",
                    _opener=lambda: np.arange(4 * 32 * 32, dtype=np.uint16).reshape(4, 32, 32))
    for name in ("bg_stats", "image_quality", "star_detect", "cosmic_reject"):
        assert analysis.get(name).handles(item), name
    assert analysis.get("dynamic_threshold").handles(item) is False
