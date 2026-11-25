import numpy as np

from mdva.training import fl_native_local_training as study


def test_native_grid_covers_entire_image_without_gaps():
    coverage = np.zeros((study.SIZE, study.SIZE), dtype=bool)
    for window in study.grid():
        assert all(axis.stop - axis.start == study.TILE for axis in window)
        coverage[window] = True
    assert coverage.all()


def test_crop_selection_keeps_a_weak_reference_and_shares_hard_background(monkeypatch):
    monkeypatch.setattr(study, 'grid', lambda: [(slice(0, 16), slice(x, x + 16)) for x in (0, 16, 32)])
    features = np.zeros((3, 16, 48), dtype=np.float32)
    target = np.zeros((16, 48), dtype=np.float32)
    valid = np.ones_like(target, dtype=bool)
    target[1:6, 1:6] = 1
    target[1:9, 17:25] = 1
    features[2, :, 0:16] = .03
    features[2, :, 16:32] = .5
    features[2, :, 32:] = 1

    assert study.pick_patches(features, target, valid) == [0, 1, 2]
