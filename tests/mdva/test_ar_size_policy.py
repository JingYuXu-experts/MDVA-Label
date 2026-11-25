import numpy as np

from mdva.training.ar_size_policy import optimized_cuts, size_indices


def test_size_bins_match_native_region_thresholds():
    rows=[{'parts':[{'area':255},{'area':256},{'area':1023},{'area':1024}]}]
    assert size_indices(rows).tolist()==[0,1,1,2]


def test_size_policy_preserves_verified_tiny_matches_and_global_budget():
    initial=np.array([.05,.10,.50,.03,.34,.4,.05,.3])
    values=np.r_[initial,np.full(120,.8)]
    tags=np.r_[[0,0,0,1,1,1,2,2],np.full(120,2)]
    truth=np.r_[[False,True,False,False,True,False,False,False],np.ones(120,dtype=bool)]
    cuts,result=optimized_cuts(values,tags,truth)
    rejected=values<cuts[tags]
    assert not rejected[1]
    assert int((rejected&truth).sum())==result['matched_removed']
    assert result['matched_removed']<=result['matched_allowed_to_remove']==2
    assert int((rejected&~truth).sum())==result['unmatched_removed']
