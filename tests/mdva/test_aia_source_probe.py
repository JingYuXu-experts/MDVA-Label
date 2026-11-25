import numpy as np
import pytest
from mdva.training.aia_source_probe import ones_sum,mapping
from mdva.web.space_weather import download_aia_synoptic


def test_fits_end_around_carry_and_bad_byte_lengths():
    assert ones_sum(b'\xff'*4)==0xffffffff
    assert ones_sum(b'\xff'*4+b'\x00\x00\x00\x01')==1
    assert ones_sum(b'\x00'*8)==0
    with pytest.raises(ValueError):ones_sum(b'bad')


def test_registered_source_does_not_accept_url_or_path_as_time():
    for stamp,channel in [('https://example.com','193'),('20190128_0601','193'),('20190128_0600','999')]:
        with pytest.raises(ValueError):download_aia_synoptic(stamp,channel)


def test_mapping_refuses_unknown_grid_or_nonphysical_radius():
    with pytest.raises(ValueError):mapping(np.ones((512,512)),{})
    with pytest.raises(ValueError):mapping(np.ones((1024,1024)),{'CDELT1':2.4,'RSUN_OBS':0})
