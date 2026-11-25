import numpy as np
import pytest

torch = pytest.importorskip('torch')

from mdva.training.ar_patch_refiner import central_mask, crop_array, loss_value


def test_refiner_retains_only_center_component_in_native_crop():
    logits = torch.full((1, 1, 8, 8), -8.)
    logits[..., 0:2, 0:2] = 8.
    logits[..., 3:6, 3:6] = 8.
    patch = central_mask(logits, [100, 200, 108, 208], [104, 204])
    assert patch.sum() == 9
    assert not patch[0, 0]
    assert not central_mask(logits, [100, 200, 108, 208], [107, 207]).any()


def test_negative_crop_remains_finite_and_has_gradient():
    empty = crop_array(np.zeros((100, 100), dtype=np.uint8), [10, 20, 30, 50], target=True)
    target = torch.from_numpy(empty)[None, None].float()
    logits = torch.zeros_like(target, requires_grad=True)
    loss = loss_value(logits, target)
    loss.backward()
    assert torch.isfinite(loss)
    assert torch.all(logits.grad > 0)
