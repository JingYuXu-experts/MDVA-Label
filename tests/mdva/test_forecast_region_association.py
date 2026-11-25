import numpy as np

from mdva.training.forecast_region_detector_audit import match_reference
from mdva.training.forecast_region_join import association


def test_join_rejects_neighboring_time_and_conflicting_noaa():
    sample = {'stem':'magnetogram', 'time_utc':'2021-04-12T00:00:00+00:00',
              'instance_id':2, 'instrument':'HMI', 'noaa':12787}
    image = {'instrument':'HMI', 'mask_statistics':{'pixels':{'2':18}}}
    key = (sample['stem'],sample['time_utc'])
    mapping = {('magnetogram',2):{12787}}
    assert association(sample, {key:[image]}, mapping) == ('exact_reference_join',image)
    assert association({**sample,'time_utc':'2021-04-12T01:00:00+00:00'}, {key:[image]}, mapping)[1] is None
    assert association(sample, {key:[image,image]}, mapping) == ('ambiguous_image', None)
    assert association(sample, {key:[image]}, {('magnetogram',2):{12787,12789}})[1] is None


def test_unmatched_reference_stays_missing_instead_of_zero_area():
    reference = np.zeros((10,10),dtype=np.int32)
    reference[1:4,1:4] = 5
    reference[6:9,6:9] = 17
    matches, count = match_reference(reference==5,reference)
    assert count==1
    assert matches=={5:{'mask_iou':1.,'detected_area_pixels':9},17:None}


def test_no_prediction_or_reference_regions():
    empty = np.zeros((5,5),dtype=np.int32)
    assert match_reference(empty>0,empty)==({},0)
    region=empty.copy()
    region[2,2]=3
    assert match_reference(empty>0,region)==({3:None},0)
