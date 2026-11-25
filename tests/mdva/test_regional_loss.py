import numpy as np
import pytest

torch=pytest.importorskip('torch')
from mdva.training.regional_loss import weight_map,loss_value
from mdva.training.reviewed_train import loss_value as baseline


def test_region_weights_preserve_mass_background_and_capped_small_region_boost():
    y=np.zeros((40,50),np.float32);y[1:31,1:31]=1;y[35:37,40:42]=1
    w=weight_map(y)
    assert np.all(w[y==0]==1) and .25<=w.min()<=w.max()<=4
    assert w[35,40]>w[1,1]
    assert float(w[y>0].sum())==pytest.approx(float(y.sum()),rel=1e-6)
    for target in (np.zeros((8,8)),np.ones((8,8))):assert np.array_equal(weight_map(target),np.ones((8,8)))
    for invalid in (np.zeros((2,2,2)),np.array([[np.nan]]),np.array([[.2]])):
        with pytest.raises(ValueError):weight_map(invalid)


def test_empty_single_region_and_validation_use_identical_baseline_loss():
    logits=torch.randn(2,1,16,16,requires_grad=True)
    y=torch.zeros_like(logits);y[:,:,2:6,3:7]=1
    assert torch.allclose(loss_value(logits,y,'bce_dice'),baseline(logits,y,'bce_dice'),atol=1e-7)
    y[:,:,10:12,10:12]=1
    with torch.no_grad():
        assert torch.equal(loss_value(logits,y,'bce_dice'),baseline(logits,y,'bce_dice'))
    loss=loss_value(logits,y,'bce_dice');loss.backward()
    assert torch.isfinite(loss) and torch.isfinite(logits.grad).all()


def test_small_region_has_more_gradient_without_extra_background_penalty():
    y=torch.zeros(1,1,30,30);y[:,:,1:20,1:20]=1;y[:,:,24:26,24:26]=1
    a=torch.zeros_like(y,requires_grad=True);b=torch.zeros_like(y,requires_grad=True)
    loss_value(a,y,'bce_dice').backward();baseline(b,y,'bce_dice').backward()
    assert a.grad[0,0,24,24].abs()>b.grad[0,0,24,24].abs()
    assert torch.equal(a.grad[0,0,29,29],b.grad[0,0,29,29])
