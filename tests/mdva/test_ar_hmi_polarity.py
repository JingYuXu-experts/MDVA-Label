import numpy as np
import pytest

pytest.importorskip('torch')

from mdva.training.ar_hmi_polarity import polarity_planes


def test_polarity_pooling_preserves_both_extremes_and_drops_incomplete_cells():
    field = np.zeros((1024, 1024), dtype=np.float32)
    field[0, :2] = [200, -200]
    field[0, 2:4] = [150, np.nan]
    field[0, 4:6] = [-2000, -100]
    mean = polarity_planes(field, 'mean')
    peaks = polarity_planes(field, 'peaks')
    assert mean.shape == peaks.shape == (2, 512, 512)
    assert np.isfinite(peaks).all()
    assert not mean[:, 0, 0].any()
    assert peaks[0, 0, 0] > 0 and peaks[1, 0, 0] == pytest.approx(-peaks[0, 0, 0])
    assert not peaks[:, 0, 1].any()
    assert peaks[1, 0, 2] == pytest.approx(-1.)
