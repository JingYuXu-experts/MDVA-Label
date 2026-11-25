from datetime import datetime, timedelta, timezone

import numpy as np

from mdva.training.ar_hmi_candidate_ranker import block
from mdva.training.ar_hmi_temporal_ranker import object_scores, policy_masks, time_partition


def test_temporal_selection_uses_chronological_blocks_with_boundary_gaps():
    start = datetime(2010, 1, 1, tzinfo=timezone.utc)
    rows = [{'time_utc': (start+timedelta(days=27*i)).isoformat()} for i in range(20)]
    parts = time_partition(rows)
    by_role = [[block(row['time_utc']) for row in parts[key]] for key in ('fit', 'policy', 'held')]
    assert [len(part) for part in by_role] == [9, 4, 5]
    assert max(by_role[0])+1 < min(by_role[1])
    assert max(by_role[1])+1 < min(by_role[2])


def test_previous_frame_unavailable_preserves_existing_candidate():
    model = {'features': ['signed_correlation'], 'mean': [0.], 'scale': [1.], 'weights': [1.],
             'intercept': 0., 'review_threshold': .5}
    image = {'candidates': [{'area': 80}, {'signed_correlation': .1}, {'signed_correlation': .8}]}
    assert policy_masks(image, {'temporal': model})['temporal'] == {
        'keep': [True, False, True], 'scores': [None, .1, .8],
    }


def test_spatially_filtered_metrics_recompute_native_matches():
    raster = np.array([[1, 1, 0, 2], [1, 1, 0, 2], [0, 0, 0, 2]])
    reference = np.where(raster == 1, 7, 0)
    parts = [{'area': 4}, {'area': 3}]
    before, _, assigned = object_scores(raster, parts, reference, [True, True])
    after, _, remaining = object_scores(raster, parts, reference, [False, True])
    assert before['matched_reference_ids'] == [7]
    assert assigned == {0}
    assert after['reference'] == after['predicted'] == 1
    assert after['matched_reference_ids'] == []
    assert remaining == set()
