import numpy as np
import pytest

from mdva.training.ch_display_audit import normalize,measure,fit_anchor


def disk_image(value):
    y,x=np.ogrid[:128,:128]
    gray=np.full((128,128),255,dtype=np.uint8)
    gray[np.hypot(x-64,y-64)<58]=value
    return gray


def test_transform_uses_only_image_preserves_geometry_and_white_background():
    gray=disk_image(100)
    transformed,info=normalize(gray,75)
    assert info['applied'] and info['factor']==.75
    assert transformed.shape==gray.shape
    assert transformed[64,64]==pytest.approx(75/255)
    assert transformed[0,0]==1 and np.array_equal(gray,disk_image(100))


def test_identity_anchor_and_unsupported_images_fall_back_without_guessing():
    gray=disk_image(100)
    result,info=normalize(gray,100)
    assert np.array_equal(result,gray.astype(np.float32)/255.)
    result,info=normalize(np.full((128,128),20,dtype=np.uint8),75)
    assert not info['applied'] and np.allclose(result,20/255)
    result,info=normalize(disk_image(10),100)
    assert not info['applied']


def test_anchor_never_fitted_with_validation_or_failed_fit_values():
    rows=[{'split':'train','available':True,'core_median':50},
          {'split':'train','available':True,'core_median':70},
          {'split':'train','available':False,'core_median':200}]
    assert fit_anchor(rows)==60
    with pytest.raises(ValueError,match='训练'):fit_anchor(rows+[{'split':'validation','available':True,'core_median':150}])
    with pytest.raises(ValueError):normalize(disk_image(100),float('nan'))
