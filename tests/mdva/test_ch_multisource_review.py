import numpy as np

from mdva.training.ch_multisource_review import classify, diagnostics


def signals():
    candidate = np.zeros((80,80),dtype=bool)
    candidate[31:49,31:49] = True
    valid = np.ones_like(candidate)
    arrays = {'193':np.where(candidate,.45,1.),'211':np.where(candidate,.6,1.),
              '171':np.ones_like(candidate,dtype=float),'hmi':np.where(candidate,12.,0.)}
    return candidate,valid,arrays


def test_multisource_suspicion_requires_darkness_polarity_and_no_reference():
    mask,valid,arrays = signals()
    result = diagnostics(mask,arrays,valid,mask)
    assert classify(result,0) == 'high_suspicion'
    assert classify(result,1) == 'reference_overlap'
    del arrays['hmi']
    assert classify(diagnostics(mask,arrays,valid,mask),0) == 'needs_review'


def test_balanced_polarities_and_missing_core_are_not_promoted():
    mask,valid,arrays = signals()
    arrays['hmi'][:40,:] *= -1
    result = diagnostics(mask,arrays,valid,mask)
    assert result['field_state'] == 'mixed_polarity'
    assert classify(result,0) == 'needs_review'
    valid[mask] = False
    assert classify(diagnostics(mask,arrays,valid,mask),0) == 'needs_review'
