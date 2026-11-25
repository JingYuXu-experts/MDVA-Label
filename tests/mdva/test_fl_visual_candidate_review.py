import json

import numpy as np
import torch

from mdva.training.fl_visual_candidate_review import ImageReviewer, crop, exact_policy, measure


def test_native_crop_centers_seed_and_model_is_finite():
    native = np.full((2048, 2048), .5, dtype=np.float32)
    seed = np.zeros((512, 512), dtype=bool)
    seed[200:204, 150:154] = True
    x = crop(native, seed, (slice(200, 204), slice(150, 154)))
    assert x.shape == (3, 96, 96)
    assert abs(float(x[2].sum()) - 36.) < 1.
    assert float(np.abs(x[:2]).max()) < 1e-5
    assert torch.isfinite(ImageReviewer()(torch.from_numpy(x[None].astype(np.float32)))).all()


def test_exact_cutoff_treats_equal_scores_as_one_transition():
    row = {'base_count': 0, 'count': 3, 'scores': {'visual': [.9, .8, .8], 'features': [.8, .8, .8]},
           'geometry': [{'reference_ids': [1, 2], 'iou': [[.8, 0.], [0., .6], [0., 0.]]}]}
    results = exact_policy([row, row])
    assert results['visual']['cutoff'] == .8
    assert results['visual']['gain'] == 4
    assert results['visual']['extra_unmatched'] == 2
    assert results['visual']['passed']
    assert json.loads(json.dumps(measure([row], 'visual', np.float64(.8))))['predicted'] == 3
