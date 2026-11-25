import numpy as np

from mdva.training.solar_self_check import assess_stability


def test_large_match_does_not_hide_lost_small_region():
    primary = np.zeros((64, 64), dtype=np.float32)
    primary[2:48, 2:48] = .9
    primary[50:52, 50:52] = .6
    repeat = primary.copy()
    repeat[50:52, 50:52] = .4
    result = assess_stability(primary, repeat, threshold=.5, polygon_skips=[])
    assert result['global_mask_iou'] > .99
    assert result['state'] == 'needs_review'
    assert result['unstable_primary_examples'] == [{'bbox_xywh':[50, 50, 2, 2], 'best_mask_iou':0.}]


def test_repeated_empty_detection_requires_review():
    mask = np.zeros((32, 32), dtype=np.float32)
    result = assess_stability(mask, mask, threshold=.5, polygon_skips=[])
    assert result['state'] == 'needs_review'
    assert result['reason_codes'] == ['empty_candidate']
    assert result['global_mask_iou'] is None


def test_consistent_region_remains_a_candidate():
    mask = np.zeros((32, 32), dtype=np.float32)
    mask[3:15, 8:20] = .9
    result = assess_stability(mask, mask, threshold=.5, polygon_skips=[])
    assert result['state'] == 'consistent_candidate'
    assert result['global_mask_iou'] == 1.
    assert result['reason_codes'] == []
