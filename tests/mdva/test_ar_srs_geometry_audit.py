import numpy as np
import pytest

pytest.importorskip('torch')

from mdva.training.ar_srs_geometry_audit import project, unique_links


def test_projection_uses_observer_latitude_and_observed_image_orientation():
    header = {'CRLN_OBS': 350, 'CRLT_OBS': 7, 'RSUN_OBS': 970}
    transform = {'adjustment_xy_scale': [0., 0., 1.], 'orientation': [1, -1]}
    assert project(7, 350, header, transform) == pytest.approx((511.5, 511.5))
    assert project(12, 350, header, transform)[1] < 511.5
    assert project(7, 0, header, transform)[0] > 511.5
    assert project(7, 170, header, transform) is None


def test_noaa_association_requires_uniqueness_in_both_directions():
    labels = np.zeros((80, 80), dtype=np.uint8)
    labels[12, 12] = 1
    labels[12, 20] = 2
    labels[62, 62] = 3
    result, ambiguous = unique_links({5111: (16., 12.), 5112: (62., 62.)}, labels)
    assert result == {3: 5112}
    assert ambiguous == 2
    assert unique_links({5111: (62., 62.), 5112: (64., 63.)}, labels) == ({}, 1)
