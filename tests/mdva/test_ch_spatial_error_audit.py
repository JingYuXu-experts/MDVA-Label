import numpy as np

from mdva.training.ch_spatial_error_audit import regions


def test_area_replay_distinguishes_true_match_from_uncovered_components():
    reference = np.zeros((72, 72), dtype=bool)
    reference[24:48, 24:48] = True
    reference[50:58, 50:58] = True
    predicted = reference.copy()
    predicted[50:58, 50:58] = False
    predicted[2:9, 2:9] = True

    analysis = regions(predicted, reference)

    assert analysis['unmatched_without_overlap'] == 1
    assert analysis['missed_refs_without_overlap'] == 1
    assert analysis['policies']['32'] == {'reference': 2, 'predicted': 2, 'matched': 1}
    assert analysis['policies']['512'] == {'reference': 2, 'predicted': 1, 'matched': 1}
