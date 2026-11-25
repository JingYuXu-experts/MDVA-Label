import numpy as np
from PIL import Image

from mdva.training.ar_candidate_crop_study import SIDE, resize_patch
from mdva.training.ar_physical_crop_study import fallback, physical_crop, quantized_field


def test_raw_field_preserves_sign_and_missing_support():
    magnetic, available = quantized_field(np.array([[-1000.,0.,1000.,np.nan]]))
    assert magnetic.tolist()==[[0,128,255,128]]
    assert available.tolist()==[[255,255,255,0]]


def test_same_view_uses_signed_field_with_measured_local_support():
    y,x=np.indices((1024,1024))
    view=(65+(2*x+y)%130).astype(np.uint8)
    available=np.full((1024,1024),255,dtype=np.uint8)
    crop=resize_patch(view,20,(511,511),128,Image.Resampling.BILINEAR)
    image=np.stack((crop,crop,np.full((SIDE,SIDE),255,dtype=np.uint8)))
    box=[501,501,521,521]
    result=physical_crop(view,available,box,image)
    assert result is not None
    np.testing.assert_array_equal(result[0],crop)
    assert physical_crop(view,np.zeros_like(available),box,image) is None


def test_missing_raw_field_reuses_the_display_decision_exactly():
    score=fallback(np.array([-.3,.3,.9]),np.array([.7,-.9,.2]),np.array([False,True,False]),
                   {'display':0.,'calibrated':.1})
    np.testing.assert_allclose(score['calibrated'],[-.3,-1.,.9])
    np.testing.assert_allclose(score['display'],[-.3,.3,.9])
