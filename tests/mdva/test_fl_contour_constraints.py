import numpy as np
from scipy.ndimage import label

from mdva.training.fl_contour_constraints import CONNECTIVITY, geometry, limited, ownership


def test_nearest_seed_and_gap_prevent_diagonal_merging():
    seeds = np.zeros((15, 15), dtype=int)
    seeds[3:5, 5:7] = 1
    seeds[7:9, 9:11] = 2
    distance, owners, interior = ownership(seeds)
    any_foreground = np.ones_like(seeds, dtype=bool)
    regions = [limited(any_foreground, seeds == i, interior & (owners == i), distance) for i in (1, 2)]
    joined = regions[0] | regions[1]
    assert label(joined, structure=CONNECTIVITY)[1] == 2
    assert joined[seeds > 0].all()
    assert geometry({'baseline': np.zeros_like(joined), 'bounded': joined}, seeds)['bounded']['merged_components'] == 0


def test_foreground_growth_respects_area_budget_and_cannot_replace_missing_anchor():
    seeds = np.zeros((15, 15), dtype=int)
    seeds[6:9, 6:9] = 1
    seed = seeds > 0
    d, _, _ = ownership(seeds)
    permitted = np.ones_like(seed)
    closed = limited(permitted, seed, permitted, d)
    assert int(seed.sum()) <= closed.sum() <= int(2 * seed.sum())
    assert not limited(~seed, seed, permitted, d).any()
