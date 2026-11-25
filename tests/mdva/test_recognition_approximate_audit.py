import numpy as np

from mdva.training.recognition_approximate_audit import pair_regions, totals


def test_approximate_match_accepts_a_three_quarter_reference_coverage():
    ref = np.zeros((12,12),dtype=np.int32)
    ref[2:6,2:6] = 17
    prediction = ref>0
    prediction[5,:] = False
    result = pair_regions(prediction,ref,'AR')
    assert result['matched'] == 1
    assert result['matching_regions'][0] == {
        'candidate':1,'reference':17,'reference_coverage':.75,'candidate_coverage':1.
    }


def test_enclosing_two_annotations_in_one_large_blob_cannot_pass():
    ref = np.zeros((12,12),dtype=np.int32)
    ref[2:4,2:4] = 17
    ref[7:9,7:9] = 93
    result = pair_regions(np.ones_like(ref,dtype=bool),ref,'AR')
    assert result['matched'] == 0
    assert result['unmatched_references'] == 2


def test_unmarked_candidate_is_exposed_for_review_and_kept_in_denominator():
    ref = np.zeros((12,12),dtype=np.int32)
    ref[2:4,2:4] = 1
    mask = ref>0
    mask[7:9,7:9] = True
    result = pair_regions(mask,ref,'FL')
    summary = totals([result])
    assert result['outside_all_reference'] == 1
    assert summary['recall'] == 1.
    assert summary['reference_precision'] == .50
    assert not summary['meets_both_80_on_evaluated_reference']
