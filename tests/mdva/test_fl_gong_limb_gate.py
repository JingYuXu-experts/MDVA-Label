import numpy as np

from mdva.training.fl_gong_limb_gate import DISTANCE, visible_disk


def test_limb_uses_sharp_interior_edge_rather_than_faint_outer_halo():
    gray = np.select([DISTANCE <= 226, DISTANCE <= 252], [.7, .19], default=.02)
    disk, radius = visible_disk(gray)

    assert radius in (225, 226, 227)
    assert disk[256, 256] and not disk[256, 500]
    assert visible_disk(np.zeros_like(gray)) == (None, None)
