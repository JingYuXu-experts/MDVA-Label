import numpy as np
import pytest

from mdva.training.fl_gong_hmi_historical import (
    corroboration_signals,
    display_alignment,
    legacy_wcs,
)


def test_legacy_geometry_uses_half_pixel_center_and_observed_radius():
    header = {'CTYPE1': 'GONGX', 'CTYPE2': 'GONGY', 'SOLAR-R': 945.,
              'FNDLMBXC': 1024., 'FNDLMBYC': 1024., 'FNDLMBMA': 900.,
              'FNDLMBMI': 900., 'FNDLMBAN': 0.}
    geometry = legacy_wcs(header)
    assert geometry['CRPIX1'] == geometry['CRPIX2'] == 1024.5
    assert geometry['CDELT1'] == geometry['CDELT2'] == pytest.approx(1.05)
    with pytest.raises(ValueError):
        legacy_wcs({**header, 'FNDLMBMI': 860.})


def test_orientation_rejects_symmetries_and_flat_input():
    intensity = np.random.default_rng(2).normal(size=(512, 512))
    result = display_alignment(intensity, intensity[::-1])
    assert result['vertical_flip'] == pytest.approx(1.)
    assert max(v for k, v in result.items() if k != 'vertical_flip') < .1
    assert all(v is None for v in display_alignment(intensity, np.zeros_like(intensity)).values())


def test_paired_neighborhood_and_controls_preserve_reference_identity():
    mask = np.zeros((12, 12), dtype=bool)
    mask[1, 3:5] = True
    distance = np.full((12, 12), 50.)
    distance[1, 3:5] = 1
    valid = np.ones_like(mask)
    tags = [{'variant': 0, 'reference_id': 1, 'area': 2, 'basis': 'corroborated_at_iou_50'}]
    row, = corroboration_signals(distance, valid, [mask], tags)
    assert row['fraction_within_3_pixels'] == 1.
    assert row['rotated_controls'] == [0.] * 3
    assert row['east_west_mirror_control'] == 0.
    valid[np.fliplr(mask)] = False
    assert not corroboration_signals(distance, valid, [mask], tags)
