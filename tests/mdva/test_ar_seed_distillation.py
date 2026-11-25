import pytest

torch = pytest.importorskip('torch')

from mdva.training.ar_seed_distillation import guide, training_loss


def test_teacher_guide_only_keeps_reference_corroborated_changes():
    probabilities = torch.tensor([[[[.1, .1, .9, .90, .39]]],
                                  [[[.9, .9, .05, .80, .41]]],
                                  [[[.8, .8, .10, .75, .42]]]])
    truth = torch.tensor([[[1, 0, 0, 1, 1]]])
    target = guide(probabilities, truth)
    assert target[0, 0].tolist() == pytest.approx([.6, -1, .35, -1, -1], abs=1e-5)


def test_no_teacher_support_reduces_exactly_to_hard_label_gradient():
    logits = torch.tensor([[[[-1., 0., 1.]]]], requires_grad=True)
    truth = torch.tensor([[[[0., 1., 1.]]]])
    target = torch.cat((truth, torch.full_like(truth, -1)), dim=1)
    plain, guided = (training_loss(logits, labels, 'bce') for labels in (truth, target))
    assert plain.item() == guided.item()
    assert torch.equal(torch.autograd.grad(plain, logits)[0], torch.autograd.grad(guided, logits)[0])
