import numpy as np
import pytest
from scipy.ndimage import gaussian_filter

pytest.importorskip('torch')

from mdva.training.ar_hmi_registration import DISPLAY_RADIUS, SIZE, register, sample
from mdva.training.ar_hmi_region_audit import measure
from mdva.training.hmi_nrt_intake import data_url


def test_registration_recovers_scale_and_orientation_on_withheld_blocks():
    rng = np.random.default_rng(2026)
    field = gaussian_filter(rng.normal(size=(SIZE, SIZE)), 2.5).astype(np.float32) * 1000
    y, x = np.indices(field.shape)
    center = (SIZE-1) / 2
    grid = (center, center, DISPLAY_RADIUS, DISPLAY_RADIUS, 0.)
    values = np.array([1.7, -2.4, 1.012])
    gray = np.clip(127.5 + sample(field, grid, (1, -1), values, x, y) * .25, 0, 255)
    result = register(gray, field, grid)
    assert result['accepted']
    assert result['orientation'] == [1, -1]
    assert result['heldout_correlation'] > .98
    assert result['adjustment_xy_scale'] == pytest.approx(values, abs=.08)
    assert abs(result['adjustment_xy_scale'][2] - 1.012) < .001


def test_region_audit_requires_local_alignment_and_keeps_signed_measurements():
    field = np.full((64, 64), -80., dtype=np.float32)
    field[:, 32:] = 120.
    gray = (127. + field / 2).astype(np.uint8)
    window = slice(24, 40), slice(24, 40)
    pixels = np.ones((16, 16), dtype=bool)
    result = measure(field, gray, window, pixels)
    assert result['state'] == 'measured'
    assert result['minimum_polarity_fraction'] == .5
    assert result['absolute_p95_gauss'] == 120.
    assert measure(field, 255-gray, window, pixels)['state'] == 'local_support_insufficient'


def test_nrt_pointer_rewrites_only_the_registered_time_and_archive():
    path = b'/data/hmi/fits/2026/11/18/hmi.M_720s_nrt.20261118_110000_TAI.fits'
    parsed_url, nominal = data_url(b'M_1k_fits http://jsoc.stanford.edu' + path + b'\n')
    assert parsed_url == 'https://jsoc1.stanford.edu' + path.decode()
    assert nominal == '20261118_110000'
    with pytest.raises(ValueError):
        data_url(b'M_1k_fits http://example.test' + path)
    with pytest.raises(ValueError):
        data_url(b'M_1k_fits http://jsoc.stanford.edu' + path.replace(b'/18/', b'/17/'))
