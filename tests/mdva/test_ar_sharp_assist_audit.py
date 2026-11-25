import numpy as np
import pytest

from mdva.training.ar_hmi_registration import DISPLAY_RADIUS
from mdva.training.ar_sharp_assist_audit import bitmap_codes, compare_magnetic_fields, coverage, patch_coordinates


def test_sharp_projection_respects_true_ccd_roll_and_display_mirroring():
    meta = {'CTYPE1':'HPLN-TAN','CTYPE2':'HPLT-TAN','CUNIT1':'arcsec','CUNIT2':'arcsec',
            'CRVAL1':0,'CRVAL2':0,'CRPIX1':601,'CRPIX2':401,'CDELT1':.5,'CDELT2':.5,
            'CROTA2':180.,'RSUN_OBS':950.}
    header = {'RSUN_OBS':950.}
    align = {'adjustment_xy_scale':[1.,-3.,1.], 'orientation':[-1,1]}
    mid_x, mid_y = 512.5, 508.5
    dx = 5*DISPLAY_RADIUS/950
    a = patch_coordinates(meta,header,align,np.array([mid_x,mid_x+dx]),np.array([mid_y,mid_y]))
    np.testing.assert_allclose(a,[[400.,400.],[600.,610.]])
    with pytest.raises(ValueError,match='标准SHARP CCD'):
        patch_coordinates({**meta,'CTYPE1':'Internal ad-hoc (HPLN-TAN)'},header,align,mid_x,mid_y)


def test_same_observation_control_distinguishes_misalignment():
    field = np.random.default_rng(2026).normal(size=(85,85))
    coverage_mask = np.zeros(field.shape,dtype=bool)
    coverage_mask[22:-22,22:-22] = True
    coords = np.indices(field.shape)
    evidence = compare_magnetic_fields(field,field,coords,coverage_mask)
    assert evidence['accepted'] and evidence['same_observation_correlation'] == pytest.approx(1.)
    assert not compare_magnetic_fields(field,field,coords+np.array([[[10]],[[0]]]),coverage_mask)['accepted']


def test_archive_coverage_excludes_already_matched_reference_regions():
    reference = np.array([[11,11,0,10],[11,11,0,10]])
    patch = np.array([[True,True,False,False],[False,False,False,True]])
    regions = coverage(patch,reference,{'matched_reference_ids':[10]})
    assert regions == [{'instance':10,'area':2,'baseline_missing':False,'harp_coverage':.5},
                       {'instance':11,'area':4,'baseline_missing':True,'harp_coverage':.5}]


def test_archive_accepts_discrete_flags_stored_as_float_but_rejects_fractional_values():
    stored = np.array([0., 1., 2., 33., 34.], dtype=np.float32)
    np.testing.assert_array_equal(bitmap_codes(stored) & 32, [0,0,0,32,32])
    with pytest.raises(ValueError, match='未知值'):
        bitmap_codes(np.array([32.9]))
