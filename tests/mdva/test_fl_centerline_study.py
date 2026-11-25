import torch

from mdva.training.fl_centerline_study import centerline_loss, soft_skeleton


def test_centerline_penalizes_gap_and_preserves_a_thin_line():
    target = torch.zeros((1, 1, 21, 21))
    target[0, 0, 10, 5:16] = 1
    fragmented = target.clone()
    fragmented[0, 0, 10, 9:12] = 0

    assert torch.equal(soft_skeleton(target), target)
    assert centerline_loss(target, target).item() == 0
    assert centerline_loss(fragmented, target).item() > 0.1


def test_centerline_loss_propagates_gradients_toward_missing_foreground():
    target = torch.zeros((1, 1, 21, 21))
    target[0, 0, 10, 5:16] = 1
    probabilities = target * 0.8 + 0.1
    probabilities[0, 0, 10, 9:12] = 0.1
    probabilities.requires_grad_()

    gradients, = torch.autograd.grad(centerline_loss(probabilities, target), probabilities)

    assert torch.isfinite(gradients).all()
    assert (gradients[0, 0, 10, 9:12] < 0).all()
