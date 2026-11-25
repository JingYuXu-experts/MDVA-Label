import pytest
from mdva.training.regional_decision import gates,paired_intervals


def result(dice,precision,half,miss):
    return {'metrics':{'dice':dice,'precision':precision},'localization':{'reference_coverage':{'half_covered_fraction':half,'zero_overlap':miss}}}


def test_gate_does_not_accept_gain_over_degraded_control_only():
    protocol={'original_candidate_anchor':{'dice':.79,'precision':.86,'half_covered_fraction':.68,'no_overlap':347},
        'continuation_gate':{'half_covered_gain_over_control_and_anchor':.03,
            'relative_no_overlap_reduction_over_control_and_anchor':.05,'maximum_dice_drop_vs_control_and_anchor':.01,'maximum_precision_drop_vs_control_and_anchor':.03}}
    control=result(.78,.85,.66,388)
    assert not all(gates(control,result(.786,.857,.665,380),protocol).values())
    assert all(gates(control,result(.80,.86,.72,320),protocol).values())


def test_paired_development_intervals_reject_changed_references_and_match_known_effect():
    a=[{'uri':f'{i}.png','time_utc':f'2020-0{i+1}-01T00:00:00+00:00','dice':.7,
        'reference':[{'bbox':[0,0,10,10],'pixels':100,'covered_fraction':0}]} for i in range(3)]
    b=[{**r,'dice':.75,'reference':[{**r['reference'][0],'covered_fraction':.8}]} for r in a]
    report=paired_intervals(a,b)
    assert report['ci95']['dice']==pytest.approx([.05,.05])
    assert report['ci95']['half_coverage_fraction']==[1,1]
    with pytest.raises(ValueError):paired_intervals(a,b+[b[0]])
    b[0]['reference'][0]['pixels']=99
    with pytest.raises(ValueError,match='参考'):paired_intervals(a,b)
