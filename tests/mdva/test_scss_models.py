import pytest
torch=pytest.importorskip('torch')
from mdva.training.scss_models import SCSSNet


def test_published_trainable_parameter_count_and_graph_features():
    model=SCSSNet('batch');assert sum(p.numel() for p in model.parameters())==7851969
    assert sum(isinstance(m,torch.nn.BatchNorm2d) for m in model.modules())==18
    assert model.encoders[0][1].eps==.001 and model.encoders[0][1].momentum==.01
    assert sum(isinstance(m,torch.nn.ConvTranspose2d) for m in model.modules())==8
    assert not any(isinstance(m,torch.nn.Sigmoid) for m in model.modules())


def test_normalization_arms_have_identical_initial_convolutions_and_finite_gradients():
    torch.set_num_threads(2);torch.manual_seed(2026);a=SCSSNet('batch');torch.manual_seed(2026);b=SCSSNet('group')
    conv=lambda m:[p.detach() for mod in m.modules() if isinstance(mod,(torch.nn.Conv2d,torch.nn.ConvTranspose2d)) for p in (mod.weight,mod.bias)]
    assert all(torch.equal(x,y) for x,y in zip(conv(a),conv(b)))
    for model in (a,b):
        x=torch.rand(2,1,32,32);y=model(x)
        assert y.shape==x.shape and torch.isfinite(y).all()
        loss=torch.nn.functional.binary_cross_entropy_with_logits(y,torch.zeros_like(y));loss.backward()
        assert all(torch.isfinite(p.grad).all() for p in model.parameters())
        model.eval()
        with torch.inference_mode():assert torch.equal(model(x),model(x))


def test_unsupported_inputs_and_unregistered_variants_fail():
    with pytest.raises(ValueError):SCSSNet('instance')
    with pytest.raises(ValueError):SCSSNet('batch',width=64)
    with pytest.raises(ValueError):SCSSNet()(torch.zeros(1,3,32,32))
