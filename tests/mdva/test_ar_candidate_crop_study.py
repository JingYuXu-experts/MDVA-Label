import numpy as np

from mdva.training.ar_candidate_crop_study import SIDE, apply_filters, candidate_inputs, thresholds_for
from mdva.training.ar_hmi_temporal_ranker import object_scores


def test_crop_preserves_candidate_at_native_edge():
    raster = np.zeros((1024,1024),dtype=np.int32)
    raster[0:8,0:8] = 1
    gray = np.full_like(raster,180,dtype=np.uint8)
    score = np.where(raster > 0,.88,.04).astype(np.float32)
    crops,features,parts = candidate_inputs(gray,score,raster)
    assert parts == [{'area':64,'bbox_xyxy':[0,0,8,8]}]
    assert crops.shape == (1,3,SIDE,SIDE)
    assert crops[0,0,0,0] == 127
    assert crops[0,1].max()>200
    assert set(np.unique(crops[0,2])) == {0,255}
    assert len(features)==1


def test_retention_cutoff_uses_only_matched_training_candidates():
    truth = np.array([True]*100+[False]*5)
    scores = {'base':np.r_[np.arange(100),[-30,-20,100,110,150]]}
    choice = thresholds_for(scores,truth)
    assert np.count_nonzero(scores['base'][truth]>=choice['base']) >= 98
    assert choice['base'] == 1.


def test_filter_preserves_one_to_one_reference_replay():
    raster = np.array([[1,1,0,2,2],[1,1,0,2,2]])
    reference = np.where(raster==2,91,0)
    baseline,geometry,_ = object_scores(raster,[{'area':4},{'area':4}],reference,[True,True])
    item = {'baseline':baseline,'geometry':geometry,'matches':[False,True]}
    result = apply_filters([item],{'rank':np.array([-.4,.8])},{'rank':.3})[0]
    assert result['objects']['rank']['matched_reference_ids'] == [91]
    assert result['objects']['rank']['predicted'] == 1
    assert result['decisions']['rank']['kept'] == [False,True]
