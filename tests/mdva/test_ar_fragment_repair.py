import numpy as np
from scipy.ndimage import label

from mdva.training.ar_fragment_repair import (
    bridge_points, close_pair, evaluate_pair_labels, outcome, pair_features, select,
)
from mdva.training.ar_hmi_temporal_ranker import object_scores
from mdva.training.ar_scale_review import native_objects


def test_short_bridge_recovers_fragmented_reference_in_native_metric():
    reference = np.zeros((40, 55), dtype=np.int32)
    reference[9:25, 7:44] = 91
    mask = reference > 0
    mask[:, 23:27] = False
    raster, count = label(mask)
    sizes = np.bincount(raster.ravel())[1:]
    baseline, geometry, _ = object_scores(raster, [{'area': int(n)} for n in sizes], reference, [True]*count)
    probabilities = (np.where(mask, .8, .23), np.where(mask, .7, .25))
    options = evaluate_pair_labels(geometry, baseline, pair_features(raster, probabilities, reference, geometry))
    assert len(options) == 1 and options[0]['safe_repair']
    assert baseline['matched'] == 0 and options[0]['new_matches'] == 1
    actual = mask.copy()
    actual[tuple(np.asarray(options[0]['bridge_yx']).T)] = True
    assert native_objects(actual, reference) == outcome(geometry, options)


def test_bridge_avoids_a_third_component_and_interacting_proposals():
    raster = np.zeros((12, 12), dtype=np.int32)
    raster[2, 1] = 1
    raster[2, 7] = 2
    raster[1, 5] = 3
    _, path = close_pair(np.array([[2,1]]), np.array([[2,7]]))
    assert bridge_points(raster, path, (1,2)) is None
    options = [{'first': 0, 'second': 2, 'bridge_yx': [[2,3]]},
               {'first': 1, 'second': 3, 'bridge_yx': [[2,4]]}]
    assert select(options, [0,1]) == options[:1]
