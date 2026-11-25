import pytest

torch=pytest.importorskip('torch')
from mdva.training.author_cnn193 import AuthorCNN193,SameUp


def test_author_graph_channels_dropout_and_native_alignment():
    model=AuthorCNN193().eval()
    assert len(model.ups)==4 and model.ups[0].conv.in_channels==384
    assert model.ups[0].conv.out_channels==384
    assert model.ups[0].conv.kernel_size==(3,3)
    assert model.decoders[0][0].in_channels==576
    assert model.out.kernel_size==(3,3)
    assert sum(isinstance(m,torch.nn.Dropout) for m in model.modules())==10
    with torch.no_grad():
        x=torch.rand(1,1,32,32)
        a=model(x);b=model(x)
    assert a.shape==x.shape and torch.equal(a,b) and torch.isfinite(a).all()


def test_tensorflow_same_transpose_crops_trailing_pixel():
    layer=SameUp(1,1).eval()
    with torch.no_grad():
        layer.conv.weight.fill_(1);layer.conv.bias.zero_()
    x=torch.zeros(1,1,2,2);x[0,0,0,0]=1
    y=layer(x)[0,0]
    expected=torch.zeros(4,4);expected[:3,:3]=1
    assert torch.equal(y,expected)


def test_backward_is_finite_in_full_precision():
    torch.set_num_threads(2)
    model=AuthorCNN193();image=torch.rand(1,1,32,32)
    loss=torch.nn.functional.binary_cross_entropy_with_logits(model(image),torch.zeros_like(image))
    loss.backward()
    assert torch.isfinite(loss) and all(torch.isfinite(p.grad).all() for p in model.parameters())
