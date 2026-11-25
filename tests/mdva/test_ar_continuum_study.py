import numpy as np
import pytest

from mdva.training.ar_continuum_geometry import inspect_pair


def display_pair():
    y, x = np.ogrid[:1024, :1024]
    inside = (x - 511.5)**2 + (y - 511.5)**2 < 475**2
    spot = (x - 605)**2 + (y - 426)**2 < 16**2
    second = (x - 398)**2 + (y - 315)**2 < 10**2
    magnetic = np.where(inside, 128, 255).astype(np.uint8)
    continuum = np.where(inside, 200, 255).astype(np.uint8)
    magnetic[spot | second] = 40
    continuum[spot | second] = 65
    return magnetic, continuum


def test_display_pair_requires_support_beyond_shared_border():
    magnetic, continuum = display_pair()
    assert inspect_pair(magnetic, continuum)['state'] == 'supported_display_pair'
    assert inspect_pair(magnetic, np.rot90(continuum))['state'] == 'texture_orientation_unconfirmed'
    assert inspect_pair(magnetic, np.roll(continuum, 8, axis=1))['state'] == 'display_frame_disagrees'


def test_optional_channel_initialization_preserves_single_image_output():
    torch = pytest.importorskip('torch')
    from mdva.training.ar_continuum_study import expand
    from mdva.training.ar_precision import guard
    from mdva.training.models import UNet

    base = guard(UNet(width=4)).eval()
    primary = torch.rand(1, 1, 32, 32)
    auxiliary = torch.rand_like(primary)
    with torch.inference_mode():
        expected = base(primary)
        expanded = expand(base)
        torch.testing.assert_close(expanded(primary), expected)
        torch.testing.assert_close(expanded(torch.cat((primary, auxiliary), dim=1)), expected)
