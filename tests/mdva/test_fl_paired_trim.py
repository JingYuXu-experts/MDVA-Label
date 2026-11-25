import numpy as np

from mdva.training.fl_paired_trim import choose_threshold, compare, targets


def test_pair_selection_can_fix_one_contour_and_preserve_its_neighbor():
    row = {'features': [None, None],
           'original': [{'reference_ids': [1, 2], 'iou': [[.4, 0], [0, .7]]}],
           'trimmed': [{'reference_ids': [1, 2], 'iou': [[.6, 0], [0, .3]]}]}
    utility, crossing = targets(row)
    assert np.allclose(utility, [1.05, -1.1])
    assert crossing.tolist() == [1, -1]
    assert compare(row, np.array([True, False])).tolist() == [1, 1, 0, 2]


def test_threshold_rejects_gains_below_prespecified_minimum():
    row = {'scores': [.6],
           'original': [{'reference_ids': [1], 'iou': [[.4]]}],
           'trimmed': [{'reference_ids': [1], 'iou': [[.6]]}]}
    assert choose_threshold([row]) == (None, .6)
    assert choose_threshold([row] * 3) == (.6, .6)
