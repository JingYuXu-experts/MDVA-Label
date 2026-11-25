import numpy as np

from mdva.training.forecast_xrs_feature_parity import ARMS, MODEL, XRS, common_minutes, score_delta


def test_matching_minute_support_preserves_each_flux_product():
    live, science = common_minutes(np.array([[60,120,180],[2.,4.,8.]]), np.array([[0,60,180],[1.,3.,9.]]))

    assert live.tolist() == [[60.,180.],[2.,8.]]
    assert science.tolist() == [[60.,180.],[3.,9.]]


def test_flux_score_drift_holds_all_non_xray_features_fixed():
    keys = list(ARMS[MODEL])
    weights = {'features':keys,'coef':[2.] * len(keys),'scale':[1.] * len(keys)}
    baseline = dict.fromkeys(keys,0.)
    new = {**baseline, XRS[0]:1., 'region_count':100.}

    assert score_delta(new,baseline,weights)['logit_delta_magnetogram_held_fixed'] == 2.
