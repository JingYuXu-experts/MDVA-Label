import numpy as np
import pytest
from mdva.training.scss_ensemble_review import equal_probability


def test_equal_ensemble_uses_probabilities_and_preserves_fixed_threshold():
    a=np.full((512,512),.9,np.float32);b=np.full_like(a,.2)
    merged=equal_probability(a,b)
    assert np.allclose(merged,.55) and (merged>=.5).all()
    assert np.all(a==.9) and np.all(b==.2)


def test_ensemble_rejects_wrong_grid_or_nonfinite_scores():
    a=np.zeros((512,512),np.float32)
    with pytest.raises(ValueError,match='同一512'):
        equal_probability(a,a[:256])
    for invalid in (float('nan'),float('inf'),1.1,-.1):
        b=a.copy();b[2,3]=invalid
        with pytest.raises(ValueError,match='非有限值或越界'):
            equal_probability(a,b)
