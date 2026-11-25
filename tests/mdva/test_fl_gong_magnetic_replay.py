import numpy as np

from mdva.training.fl_gong_magnetic_replay import supported


def test_magnetic_support_needs_nearby_transition_and_complete_coverage():
    proposals = np.zeros((14, 14), dtype=bool)
    proposals[3, 1:5] = True
    proposals[9, 1:6] = True
    proposals[3, 10:12] = True
    valid = np.ones_like(proposals)
    valid[9, 5] = False
    transition = np.zeros_like(proposals)
    transition[2, 3] = transition[9, 3] = True
    kept = supported(proposals, valid, transition)
    assert kept.sum() == 4 and kept[3, 1:5].all()
    assert not supported(proposals, valid, np.zeros_like(transition)).any()
