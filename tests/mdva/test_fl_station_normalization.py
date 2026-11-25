import numpy as np

from mdva.training.fl_station_normalization import corrected, disk_profile, disk_radius_map


def test_station_profile_fits_a_valid_disk_and_rejects_full_frame_background():
    y, x = np.ogrid[:512, :512]
    distance = np.hypot(y - 254, x - 258) / 232
    gray = np.where(distance < 1, .64 - .18 * distance, 0).astype(np.float32)

    radius = disk_radius_map(gray)

    assert radius[254, 258] < .01 and radius[0, 0] > 1
    assert disk_profile(gray, radius)['radial'][1] > disk_profile(gray, radius)['radial'][8]
    assert disk_radius_map(np.ones_like(gray)) is None


def test_station_correction_does_not_change_background_and_checks_radius():
    gray = np.full((1, 3), .5, dtype=np.float32)
    radius = np.asarray([[0., .9, 1.05]])
    reference = {'radial': [.7] * 10, 'median': .7, 'contrast': .18}
    source = {'radial': [.5] * 10, 'median': .5, 'contrast': .18}

    adjusted = corrected(gray, radius, source, reference, scale=True)

    np.testing.assert_allclose(adjusted[0, [0, 2]], [.7, .5])
    assert .5 < adjusted[0, 1] < .7
    np.testing.assert_array_equal(corrected(gray, radius, source, reference), gray)
