import torch

from mdva.training.fl_shape_objective import shape_terms, skeleton
from mdva.training.fl_shape_statistics import paired


def test_centerline_term_penalizes_break_with_finite_gradient():
    truth = torch.zeros((1, 18, 24))
    truth[:, 8, 2:22] = 1.
    valid = torch.ones_like(truth, dtype=torch.bool)
    connected = truth * 16 - 8
    broken = connected.clone()
    broken[:, 8, 10:14] = -8
    broken.requires_grad_()
    boundary, disconnected_loss = shape_terms(broken, truth, valid, True)
    _, continuous_loss = shape_terms(connected, truth, valid, True)
    assert disconnected_loss > continuous_loss
    (boundary + disconnected_loss).backward()
    assert torch.isfinite(broken.grad).all()


def test_disputed_and_inaccessible_pixels_do_not_add_edge_supervision():
    logits = torch.zeros((2, 9, 9))
    votes = torch.full_like(logits, .5)
    votes[1] = 1.
    valid = torch.ones_like(votes, dtype=torch.bool)
    valid[1] = False
    assert tuple(map(float, shape_terms(logits, votes, valid, True))) == (0., 0.)
    assert not skeleton(torch.zeros((1, 1, 9, 9))).any()


def test_paired_statistics_count_the_gained_and_lost_reference_separately():
    original = {'count': 1, 'geometry': [{'reference_ids': [1, 2], 'iou': [[.7, 0.]]}]}
    changed = {'count': 1, 'geometry': [{'reference_ids': [1, 2], 'iou': [[0., .8]]}]}
    cases = [{'file': '20190417', 'arms': {'control': original, 'edge': changed}}]
    result = paired(cases, 'edge', repeats=10)
    assert result['gained_reference_records'] == result['lost_reference_records'] == 1
    assert result['matched_delta_intervals']['recall']['interval'] == [0., 0.]
