import numpy as np

from mdva.training.fl_gong_resolution import TILE, add_isolated, stitch, windows


def test_tiling_preserves_a_constant_probability_without_seams():
    tiles = [np.full((TILE, TILE), .73, dtype=np.float32) for _ in windows()]

    np.testing.assert_allclose(stitch(tiles), .73, atol=2e-7)


def test_adding_isolated_proposals_preserves_primary_outline():
    primary = np.zeros((32, 32), dtype=bool)
    primary[8:12, 8:12] = True
    local = np.zeros_like(primary)
    local[11:14, 12:17] = True
    local[23:26, 22:27] = True

    result = add_isolated(primary, local)

    assert (result & primary).sum() == 16
    assert not result[13, 15] and result[24, 23]
