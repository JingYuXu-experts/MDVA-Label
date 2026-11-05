"""位深归一化的回归测试。

上游曾用 `(255 * img).astype(np.uint8)` 转 8 位，对 uint16 会整数回绕，
把最亮的像素变成最暗的。这里锁死正确行为。
"""

import numpy as np
import pytest

from mulimgviewer.src.bitdepth import bit_depth_of, to_uint8_display


def test_uint16_bright_pixel_stays_bright():
    """回归用例：60000 曾因 255*60000 溢出回绕成暗点。"""
    arr = np.zeros((8, 8), dtype=np.uint16)
    arr[0, 0] = 60000
    arr[1, 1] = 300
    out = to_uint8_display(arr)
    assert out.dtype == np.uint8
    assert out[0, 0] > out[1, 1] > out[2, 2]
    assert int(out[0, 0]) == pytest.approx(233, abs=1)


def test_uint16_mapping_is_monotonic_over_full_range():
    arr = np.arange(65536, dtype=np.uint16).reshape(256, 256)
    out = to_uint8_display(arr)
    flat = out.ravel()
    assert np.all(np.diff(flat) >= 0), "映射后必须保持单调，否则对比图会错乱"
    assert flat[0] == 0 and flat[-1] == 255


def test_uint8_passes_through_unchanged():
    arr = np.random.default_rng(0).integers(0, 256, (16, 16), dtype=np.uint8)
    assert np.array_equal(to_uint8_display(arr), arr)


def test_float_image_uses_actual_min_max():
    arr = np.linspace(0.0, 1.0, 256).reshape(16, 16)
    out = to_uint8_display(arr)
    assert out[0, 0] == 0 and out[-1, -1] == 255
    assert int(out[8, 0]) == pytest.approx(128, abs=2)


def test_float_outside_unit_range_is_not_clipped_to_black():
    """旧写法对 >1 的浮点数据会整体饱和成白图。"""
    arr = np.array([[0.0, 500.0], [1000.0, 1500.0]])
    out = to_uint8_display(arr)
    assert out[0, 0] == 0 and out[1, 1] == 255
    assert int(out[0, 1]) == pytest.approx(85, abs=2)


def test_signed_int_negative_minimum_maps_to_zero():
    arr = np.array([[-32768, 0], [32767, -1000]], dtype=np.int16)
    out = to_uint8_display(arr)
    assert out[0, 0] == 0
    assert out[1, 0] == 255
    assert 0 < out[1, 1] < 128


def test_nan_and_inf_do_not_poison_the_whole_image():
    arr = np.array([[np.nan, 1.0], [np.inf, -np.inf]])
    out = to_uint8_display(arr)
    assert out.shape == (2, 2)
    assert out.dtype == np.uint8
    assert np.isfinite(out).all()


def test_constant_image_returns_black_without_division_error():
    for arr in (np.full((4, 4), 7.5), np.full((4, 4), 42, dtype=np.uint16)):
        out = to_uint8_display(arr)
        assert np.all(out == 0)


def test_all_floats_equal_nan_returns_black():
    out = to_uint8_display(np.full((4, 4), np.nan))
    assert out.shape == (4, 4) and np.all(out == 0)


def test_relative_brightness_survives_across_two_images():
    """多图对比的核心前提：两张图各自映射后，亮的那张仍然看起来更亮。

    若按每张图自己的 min-max 拉伸，这个性质就不成立了。
    """
    dim = np.full((32, 32), 800, dtype=np.uint16)
    bright = np.full((32, 32), 40000, dtype=np.uint16)
    assert to_uint8_display(bright).mean() > to_uint8_display(dim).mean()


def test_bool_image_expands_to_full_range():
    out = to_uint8_display(np.array([[True, False]], dtype=bool))
    assert out.tolist() == [[255, 0]]


def test_unsupported_dtype_raises():
    with pytest.raises(TypeError):
        to_uint8_display(np.array(["a", "b"]))


@pytest.mark.parametrize("dtype,expected", [("uint8", 8), ("uint16", 16), ("int32", 32), ("float32", 32)])
def test_bit_depth_of(dtype, expected):
    assert bit_depth_of(np.zeros(4, dtype=dtype)) == expected
