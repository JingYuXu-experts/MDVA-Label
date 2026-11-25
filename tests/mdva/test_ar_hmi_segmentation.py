import numpy as np
import pytest

pytest.importorskip('torch')

from mdva.training.ar_hmi_segmentation import physical_plane


def test_signed_field_preprocessing_preserves_weak_signal_and_missing_support():
    field = np.zeros((1024, 1024), dtype=np.float32)
    field[:2, :2] = 50
    field[:2, 2:4] = -50
    field[:2, 4:6] = 500
    field[1, 5] = np.nan
    field[:2, 6:8] = 5000
    plane = physical_plane(field)
    assert plane.shape == (512, 512)
    assert np.isfinite(plane).all()
    assert 0 < plane[0, 0] < plane[0, 3]
    assert plane[0, 1] == pytest.approx(-plane[0, 0])
    assert plane[0, 2] == 0
    assert plane[0, 3] == pytest.approx(1.)
