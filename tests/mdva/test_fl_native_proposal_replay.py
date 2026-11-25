import numpy as np

from mdva.training.fl_native_proposal_replay import independent_regions, select_rule


def test_weak_regions_do_not_modify_existing_candidates():
    confidence = np.zeros((64, 64), dtype=np.float32)
    anchor = np.zeros_like(confidence, dtype=bool)
    anchor[9:12, 9:12] = True
    confidence[10:16, 10:16] = .35
    confidence[28:34, 28:34] = .25
    confidence[27:35, 23:28] = .10

    candidates, parts = independent_regions(confidence, anchor, .15)

    assert len(parts) == 1
    assert not candidates[:20].any()
    assert parts[0]['area'] == 76
    assert candidates[30, 24] == 1


def test_selection_refuses_added_false_regions_outnumbering_recovered_matches():
    result = {'whole_reviewed': {'matched': 20, 'unmatched': 30},
              'noisy': {'matched': 29, 'unmatched': 40},
              'acceptable': {'matched': 24, 'unmatched': 33}}

    assert select_rule(result) == 'acceptable'
    del result['acceptable']
    assert select_rule(result) is None
