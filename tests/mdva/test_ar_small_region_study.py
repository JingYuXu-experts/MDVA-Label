import pytest

torch = pytest.importorskip('torch')

from mdva.training.ar_small_region_study import foreground_weights, weighted_bce, weighted_target


def test_size_weights_preserve_foreground_mass_and_background():
    regions = torch.zeros((40, 40), dtype=torch.long)
    regions[3:5, 2:4] = 2
    regions[10:31, 6:29] = 10
    weight = foreground_weights(regions)
    foreground = regions > 0
    assert torch.all(weight[~foreground] == 1)
    assert float(weight[foreground].sum()) == pytest.approx(int(foreground.sum()), abs=1e-4)
    assert float(weight[3, 2] / weight[10, 6]) == pytest.approx(3)
    logits = torch.zeros((1, 1, 40, 40), requires_grad=True)
    target = weighted_target(foreground[None].float(), regions)[None]
    assert target.shape == (1, 2, 40, 40)
    weighted_bce(logits, target, 'bce').backward()
    assert float(logits.grad[0, 0, 3, 2] / logits.grad[0, 0, 10, 6]) == pytest.approx(3)


def test_background_only_map_matches_unweighted_loss():
    semantic = torch.zeros((1, 1, 16, 16))
    logits = torch.ones_like(semantic)
    weights = foreground_weights(semantic[0, 0].long())
    target = torch.cat((semantic, weights[None, None]), dim=1)
    torch.testing.assert_close(weighted_bce(logits, target, 'bce'), weighted_bce(logits, semantic, 'bce'))
