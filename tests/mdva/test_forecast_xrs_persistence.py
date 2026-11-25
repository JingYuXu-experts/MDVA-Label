import numpy as np
import pytest

pytest.importorskip('sklearn')

from mdva.training.forecast_xrs_persistence import measure


def test_persistence_audit_separates_ranking_and_fixed_threshold_hits():
    truth = np.array([0, 1, 0, 1])
    flux = np.array([-6.1, -5.1, -5.7, -5.8])
    result = measure(truth, flux, -5.75)
    assert result['average_precision'] == pytest.approx(5 / 6)
    assert (result['hits'], result['misses'], result['false_alarms']) == (1, 1, 1)
    assert measure(truth[truth == 0], flux[truth == 0], -5)['average_precision'] is None
