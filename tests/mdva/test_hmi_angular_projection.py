import numpy as np
import pytest
from mdva.training.hmi_angular_projection import angular_sample,wcs


def header(scale=2.):
    return {'CRPIX1':32.5,'CRPIX2':32.5,'CDELT1':scale,'CDELT2':scale,'CRVAL1':0.,'CRVAL2':0.,'CROTA2':0.,
        'CTYPE1':'HPLN-TAN','CTYPE2':'HPLT-TAN','CUNIT1':'arcsec','CUNIT2':'arcsec'}


def test_angular_mapping_preserves_signed_field_and_source_scale():
    y,x=np.indices((64,64));field=x-31.5
    sampled,info=angular_sample(field,header(),header(4.),(64,64))
    assert np.allclose(sampled[20:44,20:44],2*(x[20:44,20:44]-31.5),atol=1e-6)
    assert np.isnan(sampled[0,0])
    assert sampled[32,30]<0 and sampled[32,33]>0
    assert info['numerical_roundtrip_max_pixels']<1e-6


def test_missing_magnetic_values_stay_unknown():
    field=np.full((64,64),120.);field[20:30,20:30]=np.nan
    sampled,_=angular_sample(field,header(),header(),(64,64))
    assert np.isnan(sampled[25,25]) and np.isclose(sampled[40,40],120.)


def test_non_solar_or_ambiguous_units_are_rejected():
    with pytest.raises(ValueError,match='单位'):
        wcs({**header(),'CUNIT1':'deg'})
    with pytest.raises(ValueError,match='日面投影'):
        wcs({**header(),'CTYPE1':'RA---TAN'})
