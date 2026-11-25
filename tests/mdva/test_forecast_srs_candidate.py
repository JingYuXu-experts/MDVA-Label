import numpy as np
import pytest

pytest.importorskip('torch')

from mdva.training.forecast_srs_candidate import APERTURE, CANDIDATE, attach_labels, evaluate, linked_features


def test_forecast_distinguishes_mask_features_from_fixed_aperture_features():
    field = np.full((96, 96), 20., dtype=np.float32)
    field[44:52, 40:52] = -160.
    probability = np.zeros_like(field)
    probability[44:52, 40:52] = .9
    regions, stats = linked_features(probability, field, {3234: (46., 48.)})
    assert stats['spatial_links'] == 1
    assert regions[0]['features'][APERTURE[0]] == 20.
    assert regions[0]['features'][CANDIDATE[0]] == 160.
    assert regions[0]['short_number'] == 3234


def test_forecast_targets_require_unique_known_outcomes_and_same_observation():
    time = '2021-05-22T00:00:00+00:00'
    inputs = [{'split': 'validation', 'uri': 'image', 'observed_utc': time,
               'target_end_utc': '2021-05-23T00:00:00+00:00', 'xrs': {'past': 1.},
               'regions': [{'short_number': 1234, 'features': {'inside': 2.}},
                           {'short_number': 5678, 'features': {}}]}]
    labels = {('validation', 'image'): {1234: [{'time_utc': time, 'target': 1, 'noaa': 11234}],
                                       5678: [{'time_utc': time, 'target': None, 'noaa': 15678}]}}
    rows, excluded = attach_labels(inputs, labels)
    assert len(rows) == 1 and rows[0]['features'] == {'past': 1., 'inside': 2.}
    assert excluded == {'unknown_outcome': 1}
    assert evaluate(rows)['state'] == 'insufficient_known_outcomes'
    labels['validation', 'image'][1234][0]['time_utc'] = '2021-05-21T23:00:00+00:00'
    with pytest.raises(ValueError, match='目标窗口'):
        attach_labels(inputs, labels)
