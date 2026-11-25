import numpy as np
import torch

from mdva.training.fl_direct_centers import additions, focal_center, mark_centers, tile_grid


def test_center_targets_land_inside_each_nontrivial_region():
    mask = np.zeros((40, 40), dtype=bool)
    mask[:4, :4] = True
    mask[27:33, 28:34] = True
    mask[12, 10] = True
    heat = mark_centers(mask)
    peaks = heat == 1
    assert peaks.sum() == 2
    assert mask[peaks].all()


def test_focal_loss_is_finite_and_ignores_invalid_pixels():
    values = torch.full((2, 8, 8), -4., requires_grad=True)
    heat = torch.zeros_like(values)
    heat[0, 2, 3] = 1
    valid = torch.ones_like(values, dtype=torch.bool)
    valid[:, 0] = False
    loss = focal_center(values, heat, valid)
    assert torch.isfinite(loss)
    loss.backward()
    assert values.grad[0, 2, 3] < 0
    assert values.grad[1, 3, 3] > 0
    assert not values.grad[:, 0].any()
    assert max(xs.stop for _, xs in tile_grid()) == 1024


def test_direct_supplements_keep_baseline_and_reject_touching_fragments():
    old = np.zeros((20, 20), dtype=bool)
    old[3:6, 3:6] = True
    new = np.zeros_like(old)
    new[7:9, 3:6] = True
    new[14:18, 14:18] = True
    result = additions(old, new, new.astype(float), new.astype(float), [old | new])
    assert result['base_count'] == result['count'] == 1
    assert len(result['geometry'][0]['reference_ids']) == 3
    assert result['geometry'][0]['iou'][1] == [0., 0., 1.]
