import numpy as np

from mdva.training.ch_boundary_growth import candidate_masks


def test_bounded_boundary_growth_uses_connected_support_and_a_distance_limit():
    values = np.zeros((512, 512), dtype=np.float32)
    values[20:26, 20:26] = .7
    values[20:26, 26:50] = .4
    values[50:60, 50:60] = .4
    result = candidate_masks(values)
    assert result['bounded_growth'][22, 29]
    assert not result['bounded_growth'][22, 30]
    assert not result['bounded_growth'][55, 55]
    assert result['lower_threshold'][55, 55]
