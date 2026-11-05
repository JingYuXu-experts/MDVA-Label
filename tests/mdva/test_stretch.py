import numpy as np
import pytest

from mdva.render import stretch as S


def _uint16_scene():
    """背景低、源很亮的科学图像典型形态。"""
    a = np.full((64, 64), 300.0, dtype=np.uint16)
    a[32, 32] = 60000
    a[10, 10] = 45000
    return a


def test_uint16_high_values_do_not_wrap():
    """回归用例：上游曾按 (255*arr) 转 uint8，uint16 会溢出回绕成暗点。"""
    a = _uint16_scene()
    rgb, info = S.to_rgb(a, interval="minmax", stretch="linear", colormap="gray")
    assert rgb.dtype == np.uint8
    # 最亮点必须比背景亮，溢出回绕会把它变成暗点
    assert rgb[32, 32].mean() > rgb[0, 0].mean()
    assert info["vmax"] == pytest.approx(60000.0)


@pytest.mark.parametrize("interval", list(S.INTERVALS))
def test_every_interval_returns_finite_ordered_bounds(interval):
    lo, hi = S.estimate_interval(_uint16_scene().astype(float), interval)
    assert np.isfinite(lo) and np.isfinite(hi)
    assert hi > lo


@pytest.mark.parametrize("method", ["minmax", "percent", "sigma", "zscale", "median"])
def test_zscale_fallback_agrees_in_order_with_astropy(method):
    """无 astropy 的降级分支必须落在合理量级内。"""
    a = np.random.default_rng(0).normal(50, 5, 4096)
    lo, hi = S._zscale_fallback(np.sort(a))
    assert lo < np.median(a) < hi
    assert hi - lo > 0


@pytest.mark.parametrize("st", list(S.STRETCHES))
def test_every_stretch_outputs_unit_range(st):
    norm, info = S.normalize(_uint16_scene().astype(float), interval="minmax", stretch=st)
    assert norm.shape == (64, 64)
    assert norm.min() >= 0.0 and norm.max() <= 1.0
    assert "vmin" in info and "vmax" in info


def test_asinh_is_monotonic_and_compresses_high_dynamic_range():
    x = np.linspace(0, 1, 1000)
    y = S.stretch_asinh(x, softening=100.0)
    assert np.all(np.diff(y) >= -1e-12)
    assert y[0] == pytest.approx(0.0)
    assert y[-1] == pytest.approx(1.0)
    # 前半段输入应贡献超过一半输出，说明暗弱层次被拉开了
    assert y[499] > 0.5


def test_invert_reverses_luminance():
    a = _uint16_scene().astype(float)
    plain, _ = S.normalize(a, interval="minmax", stretch="linear")
    inv, _ = S.normalize(a, interval="minmax", stretch="linear", invert=True)
    assert plain[32, 32] > plain[0, 0]
    assert inv[32, 32] < inv[0, 0]


def test_bad_pixels_do_not_enter_interval_estimate():
    a = np.linspace(8.0, 12.0, 1024)
    a[500] = np.inf
    a[501] = np.nan
    a[502] = -np.inf
    _, info = S.normalize(a.reshape(32, 32), interval="minmax")
    assert info["bad_pixels"] == 3
    assert info["vmin"] == pytest.approx(8.0)
    assert info["vmax"] == pytest.approx(12.0)


def test_bad_pixels_paint_with_configured_value():
    a = np.full((8, 8), 5.0)
    a[0, 0] = np.nan
    norm, _ = S.normalize(a, interval="minmax", bad=0.5)
    assert norm[0, 0] == pytest.approx(0.5)


def test_degenerate_constant_image_does_not_divide_by_zero():
    a = np.full((16, 16), 7.0)
    norm, info = S.normalize(a, interval="zscale", stretch="linear")
    assert np.isfinite(norm).all()
    assert info["vmax"] > info["vmin"]


def test_manual_vmin_vmax_overrides_estimation():
    a = _uint16_scene().astype(float)
    _, info = S.normalize(a, vmin=1000.0, vmax=2000.0)
    assert info["vmin"] == 1000.0 and info["vmax"] == 2000.0


@pytest.mark.parametrize("name", S.colormap_names())
def test_colormaps_produce_rgb8(name):
    a = _uint16_scene().astype(float)
    rgb, _ = S.to_rgb(a, interval="percent", stretch="linear", colormap=name)
    assert rgb.dtype == np.uint8
    assert rgb.shape == (64, 64, 3)


def test_gray_colormap_is_achromatic():
    rgb, _ = S.to_rgb(np.random.default_rng(1).random((32, 32)) * 100, interval="minmax",
                      stretch="linear", colormap="gray")
    assert np.array_equal(rgb[:, :, 0], rgb[:, :, 1])
    assert np.array_equal(rgb[:, :, 1], rgb[:, :, 2])


def test_lut_is_cached_and_identical_across_calls():
    assert S.colormap_lut("viridis") is S.colormap_lut("viridis")
    assert S.colormap_lut("viridis").shape == (256, 3)


def test_unknown_interval_or_stretch_raises():
    a = _uint16_scene().astype(float)
    with pytest.raises(ValueError):
        S.estimate_interval(a, "bogus")
    with pytest.raises(ValueError):
        S.normalize(a, stretch="bogus")
