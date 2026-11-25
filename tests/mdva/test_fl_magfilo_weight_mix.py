import pytest
import torch

from mdva.training.fl_magfilo_weight_mix import blend


def test_weight_mix_changes_only_parameters_and_reuses_original_buffers():
    original = {'weight': torch.tensor([2., 4.]), 'count': torch.tensor(7)}
    adapted = {'weight': torch.tensor([6., 0.]), 'count': torch.tensor(7)}

    mixed = blend(original, adapted, {'weight'}, .5)

    torch.testing.assert_close(mixed['weight'], torch.tensor([4., 2.]))
    assert mixed['count'] is original['count']
    assert blend(original, adapted, {'weight'}, 1.)['weight'] is adapted['weight']
    with pytest.raises(ValueError, match='缓存'):
        blend(original, {**adapted, 'count': torch.tensor(8)}, {'weight'}, .5)
