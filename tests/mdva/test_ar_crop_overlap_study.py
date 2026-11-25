import numpy as np
import torch

from mdva.training.ar_crop_overlap_study import overlap_targets, rank_values, training_loss


def test_overlap_target_retains_partial_candidates_only_as_location_positives():
    item={'matches':[True,False,False],'geometry':{'reference_ids':[9],'iou':[[.7],[.24],[0.]]}}
    np.testing.assert_array_equal(overlap_targets(item),[True,True,False])


def test_conditional_head_does_not_learn_contours_for_uncovered_proposals():
    logits = torch.zeros(3,2,requires_grad=True)
    weights = {name:torch.tensor(1.) for name in ('match','overlap','conditional')}
    match,overlap = torch.tensor([1.,0.,0.]),torch.tensor([True,True,False])
    training_loss(logits,match,overlap,weights,'factorized').backward()
    assert logits.grad[2,0] > 0
    assert logits.grad[2,1] == 0
    assert logits.grad[0,1] < 0
    assert logits.grad[1,1] > 0
    scores = rank_values(torch.tensor([[2.,2.],[-2.,2.]]),'factorized')
    assert scores[0] > scores[1]
