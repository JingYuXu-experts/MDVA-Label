import numpy as np

from mdva.training.ch_dark_proposals import dark_regions


def test_dark_regions_add_an_isolated_candidate_without_duplicating_anchor():
    y, x = np.ogrid[:512, :512]
    disk = np.hypot(x - 256, y - 256) <= 232
    left = ((x - 165) / 22) ** 2 + ((y - 240) / 29) ** 2 <= 1
    right = ((x - 345) / 24) ** 2 + ((y - 260) / 31) ** 2 <= 1
    gray = np.where(disk, 175, 255).astype(np.uint8)
    gray[left | right] = 64

    regions, parts = dark_regions(gray, left, left.astype(np.float32))

    assert len(parts) == 1
    assert regions[260, 345] == 1
    assert not regions[left | ~disk].any()
    assert np.isfinite(parts[0]['features']).all()
