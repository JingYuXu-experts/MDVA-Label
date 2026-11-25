import numpy as np

from mdva.training.ch_matched_tiles import RADIUS, matching_tiles


def test_matching_tiles_pairs_similar_darkness_without_reusing_tiles():
    gray = np.full((512, 512), 190, dtype=np.uint8)
    reference = np.zeros_like(gray, dtype=bool)
    reference[240:256, 176:192] = True
    gray[240:256, 176:192] = 100
    gray[240:256, 320:336] = 103
    gray[240:256, 336:352] = 145  # geometry similar, but brightness fails the gate
    gray[RADIUS > 200] = 100  # identical brightness outside the verified core

    [pair] = matching_tiles(gray, reference)

    assert pair['reference'] == 15 * 32 + 11
    assert pair['control'] == 15 * 32 + 20
    assert pair['jpg_193_delta'] < .05
