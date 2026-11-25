import json

import numpy as np
import pytest

from mdva.training.fl_gong_limb_gate import DISTANCE
from mdva.training.fl_halpha_limb_replay import keep_limb, local_features, measure


def test_local_contrast_ignores_off_disk_darkness_and_other_candidates():
    radii = 225
    regions = np.zeros((512, 512), dtype=int)
    regions[255, 466:472] = 1
    regions[252, 468:471] = 2
    gray = np.where(DISTANCE <= radii, .65, 0.)
    gray[regions > 0] = .3
    first, second = local_features(regions, gray, radii)
    assert first[0] > .9 and second[0] > .9
    assert first[1] == second[1] == 1.
    assert first[3] == second[3] == pytest.approx(.35)


def test_limb_metrics_use_same_complete_scene_and_keep_baseline():
    row = {'base_count': 1, 'limb_candidates': [True, False], 'references': [
        {'reference_ids': [1, 2], 'radius': [.7, .8], 'iou': [[1., 0.], [0., .5], [0., 0.]]}]}
    selected = keep_limb(row, np.array([.7, .99]), .5)
    assert selected.tolist() == [True, False]
    old = measure([row], [[False, False]])
    new = measure([row], [selected])
    assert old['matched'] == 1 and new['matched'] == 2
    assert old['limb_complete_misses'] == 1 and new['limb_complete_misses'] == 0
    assert new['limb_new_matched'] == 1
    assert json.loads(json.dumps(new))['predicted'] == 2
