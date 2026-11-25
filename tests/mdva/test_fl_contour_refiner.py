import numpy as np
import torch

from mdva.training.fl_contour_refiner import anchored, balanced_loss, supervising_candidates


def test_refiner_keeps_only_connected_contour_touching_candidate_and_permitted_area():
    support = np.ones((12, 12), dtype=bool)
    proposed = np.zeros_like(support)
    seed = proposed.copy()
    seed[2, 2:5] = True
    proposed[2, 1:7] = True
    proposed[8, 6:11] = True
    support[2, 6] = False
    retained = anchored(proposed, seed, support)
    assert retained.sum() == 5 and retained[2, 1:6].all()
    assert not anchored(proposed, np.roll(seed, 1, axis=0), support).any()
    assert not anchored(proposed, np.zeros_like(seed), support).any()


def test_candidate_with_disagreeing_annotators_is_not_a_negative():
    candidate = np.zeros((12, 12), dtype=int)
    candidate[2, 2:6] = 1
    candidate[8, 2:6] = 2
    first = candidate == 1
    second = np.zeros_like(first)
    yes, no, _, _ = supervising_candidates(candidate, 2, [first, second])
    assert not len(yes)
    assert no.tolist() == [1]
    yes, _, _, _ = supervising_candidates(candidate, 2, [first, first])
    assert yes.tolist() == [0]


def test_balanced_contour_loss_preserves_signal_from_single_dark_pixel():
    logits = torch.zeros(2, 1, 8, 8, requires_grad=True)
    target = torch.zeros_like(logits)
    target[0, 0, 2, 3] = 1.
    support = torch.ones_like(logits)
    support[0, 0, 0, 0] = 0.
    balanced_loss(logits, target, support).backward()
    assert logits.grad[0, 0, 2, 3] < -.1
    assert logits.grad[1, 0, 1, 1] > 0
    assert logits.grad[0, 0, 0, 0] == 0
