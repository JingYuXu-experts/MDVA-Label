import numpy as np

from mdva.training.forecast_ar_event_attribution import event_result, find_reference


def test_flaring_region_mapping_rejects_an_ambiguous_label_even_with_a_detection():
    native = np.array([[0,9],[4,0]])
    assert find_reference(12812,'img',native,{('img',9):{12812},('img',4):{12812}}) is None
    assert find_reference(12812,'img',native,{('img',9):{12812,12813}}) is None


def test_numbered_flare_requires_reference_and_matching_prior_projection():
    event = {'id':'A','start':'2021-02-05T01:00:00+00:00','class':'M1.1',
             'resolved_noaa':12812,'region_basis':'numbered'}
    reference = np.array([[0,9],[0,0]])
    projections = {'issue':{'state':'ready','positions':{2812:(2.,2.)},'links':{2:2812}}}
    evidence = event_result(event,'img',reference,reference>0,{('img',9):{12812}},{2:9},projections)
    assert evidence['reference_state']=='strict_match'
    assert evidence['srs']['issue']=='agrees_with_event_and_reference'
    evidence = event_result({**event,'resolved_noaa':None},'img',reference,reference>0,
                            {('img',9):{12812}},{2:9},projections)
    assert evidence['reference_state']=='event_noaa_unresolved' and not evidence['srs']
