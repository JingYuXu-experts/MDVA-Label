import pytest
import torch

from mdva.training.fl_author_model import loss_value
from mdva.training.fl_magfilo_local_replay import preservation_loss, replay_loss


def test_replay_uses_first_channel_as_annotation_and_only_distills_with_teacher():
    logits = torch.tensor([[[[1., -3.]]]], requires_grad=True)
    annotations = torch.tensor([[[[1., 0.]]]])
    teacher = logits.detach().sigmoid()
    soft_target = torch.cat((annotations, teacher), dim=1)

    assert replay_loss(logits, annotations, 10).item() == loss_value(logits, annotations, 10).item()
    torch.testing.assert_close(replay_loss(logits, soft_target, 10), loss_value(logits, annotations, 10))
    assert preservation_loss(-logits, teacher).item() > preservation_loss(logits, teacher).item()


@pytest.mark.skipif(not torch.cuda.is_available(), reason='CUDA autocast unavailable')
def test_replay_loss_supports_cuda_autocast():
    logits = torch.tensor([[[[1., -3.]]]], device='cuda', requires_grad=True)
    old = torch.tensor([[[[.9, .01]]]], device='cuda')
    target = torch.cat((old >= .5, old), dim=1)

    with torch.autocast('cuda', dtype=torch.float16):
        loss = replay_loss(logits, target, 10)
    loss.backward()

    assert torch.isfinite(loss) and torch.isfinite(logits.grad).all()
