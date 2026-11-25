import torch

from mdva.training.ch_spatial_multichannel import loss
from mdva.training.fl_native_positive_weight import weighted_loss


def test_positive_weight_only_updates_annotated_pixels_inside_support():
    logits = torch.zeros((1, 1, 2, 2), requires_grad=True)
    truth = torch.tensor([[[[1., 0.], [1., 0.]]]])
    valid = torch.tensor([[[[1., 1.], [0., 0.]]]])
    standard, = torch.autograd.grad(loss(logits, truth, valid), logits)
    weighted, = torch.autograd.grad(weighted_loss(logits, truth, valid), logits)

    assert weighted[0, 0, 0, 0] < standard[0, 0, 0, 0]
    assert weighted[0, 0, 0, 1] == standard[0, 0, 0, 1]
    assert not weighted[valid == 0].any()
