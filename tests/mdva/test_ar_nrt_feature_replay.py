import numpy as np
from scipy.special import expit

from mdva.training.ar_nrt_feature_replay import ARM, ARMS, RAW, REGIONS, feature_drift, mask_agreement


def test_feature_change_holds_xrs_fixed_and_bounds_the_score_response():
    keys = list(ARMS[ARM])
    weight = {'features': keys, 'scale': [1.] * len(keys), 'coef': [2.] * len(keys)}
    a = dict.fromkeys((*RAW, *REGIONS), 0.)
    b = {**a, 'region_count': 1., 'abs_contrast_q99': .25}

    drift = feature_drift(a, b, weight)

    assert drift['regional_logit_change'] == 2.
    assert drift['total_logit_change_with_xrs_fixed'] == 2.5
    assert drift['max_possible_probability_change'] >= abs(expit(2.5)-expit(0.))


def test_mask_agreement_reports_added_component_without_calling_it_wrong():
    ref = np.zeros((64,64), dtype=bool)
    ref[8:16,8:16] = True
    other = ref.copy()
    other[32:40,32:40] = True

    result = mask_agreement(ref, other)

    assert result['definitive_regions'] == result['paired_iou_ge_50'] == 1
    assert result['nrt_regions'] == 2
    assert np.isclose(result['pair_dice'],2/3)
