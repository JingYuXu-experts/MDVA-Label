import numpy as np

from mdva.training.fl_shape_replay import bridge_contours, trim_contours


def test_short_dark_supported_gap_is_joined():
    original = np.zeros((25, 25), dtype=bool)
    original[12, 1:8] = original[12, 11:18] = True
    score = np.full(original.shape, .35)
    darkness = np.full(original.shape, .10)
    valid = np.ones_like(original)
    bridged, count = bridge_contours(original, score, darkness, valid)
    assert count == 1 and bridged[12, 1:18].all()
    darkness[12, 8:11] = -.10
    unchanged, count = bridge_contours(original, score, darkness, valid)
    assert count == 0 and np.array_equal(unchanged, original)


def test_bridge_does_not_contact_a_third_component():
    mask = np.zeros((25, 25), dtype=bool)
    mask[12, 1:8] = mask[12, 11:18] = True
    mask[11, 9] = True
    score = np.full(mask.shape, .35)
    dark = np.full(mask.shape, .10)
    result, count = bridge_contours(mask, score, dark, np.ones_like(mask))
    assert count == 0 and np.array_equal(result, mask)


def test_trim_weak_edge_preserves_a_strong_core():
    mask = np.zeros((14, 14), dtype=bool)
    mask[3:7, 2:12] = True
    scores = np.full(mask.shape, .51)
    darkness = np.zeros_like(scores)
    darkness[3:6, 2:12] = .15
    result = trim_contours(mask, scores, darkness)
    assert result[3:6, 2:12].all()
    assert result.sum() == 30
