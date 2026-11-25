import numpy as np

from mdva.training.fl_swefil_paired_domain import score_instances
from mdva.training.fl_visual_candidate_review import measure


def test_author_instance_scoring_preserves_overlap_and_separate_mask_parts():
    first = np.zeros((12, 12), dtype=bool)
    first[1:4, 1:4] = True
    first[10, 10] = True
    second = np.zeros_like(first)
    second[3:6, 3:6] = True
    predicted = np.zeros_like(first)
    predicted[1:4, 1:4] = True
    frame = score_instances(predicted, [first, second], ['id-a', 'id-b'])
    geometry = frame['geometry'][0]
    assert geometry['reference_sizes'] == [10, 9]
    assert np.allclose(geometry['iou'], [[.9, 1/17]])
    assert measure([frame], 'all', 0.)['matched'] == 1
