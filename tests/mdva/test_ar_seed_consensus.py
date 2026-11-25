import numpy as np
import pytest

pytest.importorskip('torch')

from mdva.training.ar_seed_consensus import masks


def test_seed_consensus_distinguishes_majority_and_mean_and_preserves_control():
    values = np.zeros((3, 1024, 1024), dtype=np.float32)
    values[:, 10:30, 10:30] = np.array([.95, .2, .2])[:, None, None]
    values[:, 40:60, 40:60] = np.array([0., .41, .41])[:, None, None]
    values[:, 70:74, 70:74] = 1.
    result = masks(values)
    assert result['control'][10, 10] and not result['control'][40, 40]
    assert result['mean'][10, 10] and not result['mean'][40, 40]
    assert result['majority'][40, 40] and not result['majority'][10, 10]
    assert not any(mask[70, 70] for mask in result.values())
