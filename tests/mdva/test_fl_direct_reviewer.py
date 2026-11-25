import json

import numpy as np

from mdva.training.fl_direct_reviewer import region_features, select_retained
from mdva.training.fl_visual_candidate_review import measure


def test_review_features_exclude_dark_background_outside_solar_disk():
    regions = np.zeros((512, 512), dtype=int)
    regions[248:252, 463:471] = 1
    y, x = np.ogrid[:512, :512]
    valid = np.hypot(y - 255.5, x - 255.5) <= 220
    gray = np.where(valid, .6, 0.)
    gray[regions > 0] = .25
    feats, = region_features(regions, gray, .7 * (regions > 0), .3 * (regions > 0),
                             np.zeros_like(valid), valid, 220)
    assert len(feats) == 15 and np.isfinite(feats).all()
    assert np.isclose(feats[9], .35)


def test_retention_uses_only_necessary_observed_score_cutoff():
    matching = np.vstack((np.eye(10), np.zeros((2, 10))))
    raw = [.9] * 9 + [.2, .2, .1]
    row = {'base_count':0, 'count':12, 'geometry':[{'reference_ids':list(range(10)), 'iou':matching.tolist()}],
           'scores':{k:raw for k in ('centers','contour','learned')}}
    choices = select_retained([row])
    assert choices['learned'] == .9
    result = json.loads(json.dumps(measure([row], 'learned', choices['learned'])))
    assert result['matched'] == result['predicted'] == 9
