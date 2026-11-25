import numpy as np

from mdva.training.ch_seed_ensemble import prepare_masks


def test_ch_ensemble_uses_two_seed_votes_for_an_isolated_new_region():
    stack = np.zeros((3, 512, 512), dtype=np.float32)
    stack[0, 60:70, 60:70] = .9
    stack[1:, 125:135, 125:135] = .9

    results = prepare_masks(stack)

    assert results['single_seed'][65, 64] and not results['single_seed'][130, 130]
    assert results['mean_three'][130, 130] and results['median_three'][130, 130]
    assert results['isolated_consensus'][65, 64] and results['isolated_consensus'][130, 130]
