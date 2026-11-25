import numpy as np
import pytest

from mdva.training.localization_audit import objects,match_boxes,aggregate,coverage


def test_native_boxes_and_disconnected_reference_identity():
    mask=np.zeros((20,30),int);mask[2:5,3:7]=9;mask[10:12,15:18]=9
    assert len(objects(mask))==2
    result=objects(mask,instances=True)
    assert result==[{'bbox':[3,2,15,10],'pixels':18}]
    assert objects(np.zeros((3,3)))==[]


def test_localization_one_to_one_and_empty_cases():
    box={'bbox':[2,3,5,4],'pixels':20}
    matched=match_boxes([box,box],[box],.5)
    assert matched['matched']==1 and matched['false_positive']==1 and matched['missed']==0
    assert match_boxes([],[box],.5)['missed']==1
    assert match_boxes([box],[],.5)['false_positive']==1
    assert match_boxes([],[],.5)['matched']==0
    with pytest.raises(ValueError):match_boxes([],[],0)


def test_box_overlap_does_not_claim_mask_accuracy():
    # Identical extents can enclose very different segmentation shapes.
    reference=[{'bbox':[0,0,10,10],'pixels':20,'size_bin':'small_lt64','covered_fraction':.5}]
    predicted=[{'bbox':[0,0,10,10],'pixels':100,'reference_overlap_fraction':.1}]
    row={'dice':.2,'reference':reference,'predicted':predicted,'matches':{str(t):match_boxes(predicted,reference,t) for t in (.3,.5)}}
    result=aggregate([row])
    assert result['dice_mean']==.2 and result['box_iou_0.5']['recall']==1
    assert result['size_recall_at_box_iou_0.5']['small_lt64']['matched']==1


def test_pixel_coverage_separates_fragmented_detection_from_total_miss():
    mask=np.zeros((10,10),int);mask[1:3,1:5]=19;mask[6:8,6:8]=23
    pred=np.zeros_like(mask,dtype=bool);pred[1:3,1:3]=True;pred[0,9]=True
    reference,support=coverage(pred,mask,instances=True)
    assert reference==[.5,0] and sorted(support)==[0,1]
    assert coverage(np.zeros_like(pred),mask,True)==([0,0],[])
