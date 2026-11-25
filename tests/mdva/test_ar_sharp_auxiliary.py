import numpy as np
import torch
from torch import nn

from mdva.training.ar_sharp_auxiliary import dual_head, guide_target, training_loss


def test_training_guide_ignores_uncorroborated_sharp_and_reference_pixels():
    instances = np.zeros((1024,1024),dtype=np.int32)
    instances[100:108,100:108] = 1
    instances[126:130,110:114] = 2
    sharp = np.zeros_like(instances,dtype=bool)
    sharp[100:108,100:104] = True
    sharp[112:116,120:124] = True
    target,counts = guide_target(instances,sharp,{1})
    assert dict(counts) == {'corroborated':1,'missed_corroborated':1}
    assert target[51,51] == 1
    assert target[51,52] == -1  # Human mask outside the archive's range.
    assert target[56,60] == -1  # Archive detection with no human mask.
    assert target[63,55] == -1  # An adjacent, unconfirmed reference region.
    assert target[62,50] == 0
    assert target[0,0] == -1


def test_auxiliary_head_does_not_change_initial_primary_output_or_inference_shape():
    class SmallModel(nn.Module):
        def __init__(self):
            super().__init__()
            self.out = nn.Conv2d(3,1,1)

        def forward(self,image):
            return self.out(image)

    model = SmallModel()
    image = torch.randn(2,3,6,8)
    expected = model(image).detach()
    model = dual_head(model)
    assert model.train()(image).shape[1] == 2
    torch.testing.assert_close(model.eval()(image),expected)


def test_unknown_auxiliaries_have_no_gradient_and_are_not_background():
    values = torch.zeros(1,2,1,3,requires_grad=True)
    target = torch.tensor([[[[1.,0.,1.]],[[1.,0.,-1.]]]])
    training_loss(values,target,'bce_dice').backward()
    assert values.grad[0,1,0,0] < 0
    assert values.grad[0,1,0,1] > 0
    assert values.grad[0,1,0,2] == 0
    values.grad.zero_()
    target[:,1] = -1.
    training_loss(values,target,'bce_dice').backward()
    assert torch.all(values.grad[:,1] == 0)
