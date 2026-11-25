import numpy as np

from mdva.training.fl_gong_hmi_geometry import reduced, weak_pil
from mdva.training.hmi_angular_projection import wcs


def test_reduced_fits_grid_has_same_pixel_centers():
    header = {'CTYPE1':'HPLN-TAN','CTYPE2':'HPLT-TAN','CRPIX1':1024.5,'CRPIX2':1024.5,
              'CUNIT1':'arcsec','CUNIT2':'arcsec','CRVAL1':0.,'CRVAL2':0.,
              'CDELT1':1.,'CDELT2':1.,'CROTA1':0.,'CROTA2':0.}
    x = np.array([97., 222., 500.])
    y = np.array([103., 250., 492.])

    assert np.allclose(wcs(header).all_pix2world(4*x+1.5, 4*y+1.5, 0),
                       wcs(reduced(header)).all_pix2world(x,y,0), atol=1e-10)


def test_sign_transition_requires_both_signs_and_finite_coverage():
    field = np.broadcast_to((np.arange(64)-32)*8., (64,64)).copy()
    valid = np.ones(field.shape, dtype=bool)
    valid[:8] = False
    field[~valid] = np.nan

    trace = weak_pil(field, valid)

    assert trace[32, 32]
    assert not trace[4].any()
    assert not weak_pil(abs(field), valid).any()
