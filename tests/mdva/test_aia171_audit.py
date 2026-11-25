import pytest
from mdva.training.aia171_audit import compatibility, GRID, source_rows


def test_source_triplet_needs_same_grid_quality_and_observation_time():
    header={k:1 for k in GRID}
    header.update(WAVELNTH=171,QUALITY=0,LVL_NUM=1.5,T_OBS='2013-07-09T06:00:10Z')
    reference={**header,'T_OBS':'2013-07-09T06:00:02Z'}
    assert compatibility(header,reference)['source_gate_passed']
    wrong={**header,'T_OBS':'2013-07-09T06:02:10Z'}
    assert not compatibility(wrong,reference)['source_gate_passed']
    wrong={**header,'CDELT1':2}
    assert not compatibility(wrong,reference)['source_gate_passed']
    with pytest.raises(ValueError,match='质量'):
        compatibility({**header,'QUALITY':1},reference)
    with pytest.raises(ValueError,match='UTC'):
        compatibility({**header,'T_OBS':'2013-07-09T06:00:10'},reference)


def test_partial_source_audit_keeps_missing_date_without_substitution():
    protocol = {'rows': [{'uri': 'original-a'}, {'uri': 'original-b'}]}
    summary = {'state': 'completed', 'source_review_passed': 1, 'results': [
        {'uri': 'original-a', 'alignment_review_gate_passed': True},
        {'uri': 'original-b', 'state': 'failed', 'error': 'HTTP 404'},
    ]}

    with pytest.raises(ValueError, match='完成193/211'):
        source_rows(protocol, summary, False)
    assert source_rows(protocol, summary, True) == [(protocol['rows'][0], None), (protocol['rows'][1], 'HTTP 404')]
