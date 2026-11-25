import numpy as np

from mdva.training.ar_hmi_candidate_neighbors import carrington_drift, compare_fields, inverse_disk, nearby, projected_disk
from mdva.training.ar_srs_geometry_audit import project


def test_neighbor_projection_roundtrip_and_limb_rejection():
    header = {'CRLT_OBS': -6.1, 'CRLN_OBS': 358.2, 'RSUN_OBS': 976.}
    registration = {'orientation': [-1, 1], 'adjustment_xy_scale': [8., -3., .995]}
    expected = project(22., 3., header, registration)
    lat, lon, mu = inverse_disk(*expected, header, registration)
    np.testing.assert_allclose(np.rad2deg(lat), 22., atol=1e-9)
    np.testing.assert_allclose(np.rad2deg(lon) % 360, 3., atol=1e-9)
    assert mu > .8

    later = {**header, 'CRLN_OBS': 345., 'RSUN_OBS': 983.}
    x, y, _ = projected_disk(lat, lon, later, registration)
    np.testing.assert_allclose([x, y], project(22., 3., later, registration), atol=1e-9)
    assert np.isnan(inverse_disk(0., 0., header, registration)[2])


def test_field_comparison_requires_coverage_and_preserves_polarity():
    current = np.arange(100, dtype=float).reshape(10, 10) - 50
    support = np.ones_like(current, dtype=bool)
    np.testing.assert_allclose(compare_fields(current, -current, support)['signed_correlation'], -1)
    support[:2] = False
    assert compare_fields(current, current, support)['state'] == 'insufficient_geometry'


def test_srs_neighbor_uses_distance_to_candidate_not_its_center():
    result = nearby({1234: (10, 20), 1235: (29, 20), 1236: (100, 60)}, [10, 10, 20, 30])
    assert result == [{'short_number': 1234, 'box_distance_pixels': 0.},
                      {'short_number': 1235, 'box_distance_pixels': 10.}]


def test_howard_drift_in_carrington_frame_matches_reference_rate():
    latitude = np.deg2rad(30.)
    expected = 27.36432679 - 2 * 14.1844
    np.testing.assert_allclose(np.rad2deg(carrington_drift(latitude, 2 * 86400)), expected, atol=1e-7)
    assert carrington_drift(latitude, -86400) > 0
    assert carrington_drift(0., 86400) > 0
