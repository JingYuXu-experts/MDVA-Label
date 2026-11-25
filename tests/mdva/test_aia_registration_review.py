import numpy as np
import pytest
from mdva.training.aia_registration_review import compare


def test_registration_scan_reports_displacement_without_modifying_inputs():
    rng=np.random.default_rng(2026)
    source=rng.uniform(0,200,(512,512)).astype('uint8')
    observed=np.roll(source,1,axis=1)
    before=source.copy();original_observed=observed.copy()
    result=compare(observed,source)
    assert result['minimum_mae_shift']=={'dx':1.,'dy':0.,'core_mae_8bit':0.}
    assert result['shift_applied'] is False
    assert np.array_equal(source,before) and np.array_equal(observed,original_observed)


def test_registration_scan_rejects_unregistered_grids():
    with pytest.raises(ValueError,match='512'):
        compare(np.zeros((256,256)),np.zeros((256,256)))
