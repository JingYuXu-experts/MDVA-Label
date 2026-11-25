import numpy as np
import torch

from mdva.training.ch_spatial_multichannel import architecture, loss, normalized


def test_spatial_arms_share_inputs_and_only_clear_the_two_ratio_channels():
    data = np.arange(64, dtype=np.float32).reshape(4, 4, 4)
    support = np.ones((4, 4), dtype=bool)
    support[0, 0] = False
    center = np.full(4, 1.)
    scale = np.full(4, 2.)
    single = normalized(data, support, center, scale, 'aia193')
    triplet = normalized(data, support, center, scale, 'aia171_193_211')

    assert np.array_equal(single[:2], triplet[:2])
    assert not single[2:].any()
    assert not triplet[:, 0, 0].any()
    model = architecture()
    assert model(torch.zeros((1, 4, 32, 32))).shape == (1, 1, 32, 32)


def test_unverified_pixels_contribute_no_training_gradient():
    logits = torch.zeros(1, 1, 2, 2, requires_grad=True)
    truth = torch.tensor([[[[0., 1.], [0., 1.]]]])
    support = torch.tensor([[[[0., 1.], [1., 0.]]]])

    loss(logits, truth, support).backward()

    assert torch.all(logits.grad[support == 0] == 0.)
    assert logits.grad[0, 0, 0, 1] < 0
    assert logits.grad[0, 0, 1, 0] > 0
