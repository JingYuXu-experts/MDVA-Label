import numpy as np

from mdva.training.fl_contour_families import cutoff, ownership_overlap, suppress


def test_nested_threshold_contours_are_suppressed_without_dropping_separate_region():
    low = np.zeros((18,18),dtype=int)
    low[2:8,2:8] = 1
    low[10:15,10:15] = 2
    high = np.zeros_like(low)
    high[3:7,3:7] = 1
    overlap = ownership_overlap([low, high])
    assert overlap[0,2] == overlap[2,0] == 1.
    assert overlap[1,2] == 0.
    assert suppress([.7,.8,.9],overlap) == [2,1]


def test_contour_policy_keeps_tied_scores_and_uses_development_minimum():
    candidate = {'base_count':0,'count':3,'scores':{'review':[.8,.7,.7]},
                 'geometry':[{'reference_ids':[1,2],'iou':[[.6,0],[0,.55],[0,0]]}]}
    assert cutoff([candidate],1) == .8
    assert cutoff([candidate],2) == .7
    assert cutoff([candidate],3) is None
