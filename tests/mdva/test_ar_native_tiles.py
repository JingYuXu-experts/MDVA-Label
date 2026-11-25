import numpy as np
import pytest

pytest.importorskip('torch')

from mdva.training.ar_native_tiles import SIDE, add_isolated, blend_tiles, fixed_grid


def test_tile_blending_preserves_constant_inputs_including_outer_edges():
    grid = fixed_grid()
    values = np.full((len(grid), SIDE, SIDE), .625, dtype=np.float32)
    assert np.allclose(blend_tiles(values, grid), .625)


def test_isolated_supplements_only_add_without_joining_primary_components():
    primary = np.zeros((12, 12), dtype=bool)
    primary[2:4, 2:4] = True
    local = np.zeros_like(primary)
    local[2:4, 4:6] = True
    local[7:10, 7:10] = True
    result = add_isolated(primary, local)
    assert (result[primary]).all()
    assert not result[2:4, 4:6].any()
    assert result[7:10, 7:10].all()
