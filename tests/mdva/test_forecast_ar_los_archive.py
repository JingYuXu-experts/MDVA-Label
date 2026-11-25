import numpy as np

from mdva.training.forecast_ar_los_archive import ap_interval, gradients, physical_features


def test_mixed_polarity_gradient_requires_confident_field_on_both_sides():
    field = np.full((45,45),150.,dtype=np.float32)
    assert not gradients(field).any()
    # Smooth, monotone transition has an unambiguous neutral pixel.
    field = np.broadcast_to(np.clip((np.arange(45)-22)*55.,-180.,180.),(45,45)).copy()
    assert gradients(field)[22,22] > 0
    field[:,19:26] = np.nan
    assert not gradients(field)[:,19:26].any()


def test_region_and_rotated_comparator_have_identical_measured_area():
    field = np.zeros((1024,1024),dtype=np.float32)
    mask = np.zeros_like(field,dtype=bool)
    mask[400:410,500:510] = True
    field[mask] = 160.
    field[400,500] = np.nan
    measured, area = physical_features(field,mask)
    assert area['active'] == area['rotated'] == 99
    assert measured['active_strong_fraction'] == 1.
    assert measured['rotated_strong_fraction'] == 0.


def test_paired_ap_interval_uses_same_blocks_for_both_models():
    target = np.array([0, 1, 0, 1])
    ahead = np.array([.1,.8,.2,.9])
    result = ap_interval(target,ahead,ahead,[1,1,2,2],repeats=20)
    assert result['difference'] == 0. and result['ci95'] == [0.,0.]
    assert result['valid_repeats'] == 20
