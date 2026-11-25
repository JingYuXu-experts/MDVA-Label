import numpy as np

from mdva.training.fl_ensemble_study import overlap_profile


def test_overlap_profile_distinguishes_loose_location_and_strict_outline():
    reference = np.zeros((30, 30), dtype=np.int32)
    reference[2:7, 2:12] = 1
    reference[19:25, 16:22] = 2
    predicted = np.zeros_like(reference, dtype=bool)
    predicted[2:7, 8:12] = True

    assert overlap_profile(predicted, reference) == {
        'zero_overlap_references': 1, 'matched_iou_030': 1, 'matched_iou_050': 0,
    }


def test_overlap_profile_counts_each_reference_at_most_once():
    reference = np.zeros((20, 20), dtype=np.int32)
    reference[5:8, 3:17] = 1
    predicted = reference.astype(bool)
    predicted[:, 10] = False

    assert overlap_profile(predicted, reference) == {
        'zero_overlap_references': 0, 'matched_iou_030': 1, 'matched_iou_050': 1,
    }
