from datetime import timedelta

import pytest

from mdva.training.forecast_delayed_windows import delay_window, relabel
from mdva.training.forecast_supervision_audit import Supervisor


def test_delayed_prediction_relabels_both_ends_of_the_same_horizon():
    class ContinuousCoverage:
        def window(self,start,end):
            return {'complete':True}

    eligible = {'uri':'@data/magnetogram.jpg','split':'validation','noaa':12804,
                'observed_utc':'2021-02-01T00:00:00+00:00','target_end_utc':'2021-02-02T00:00:00+00:00',
                'target':1,'features':{}}
    start,end = delay_window(eligible)
    assert start.hour == end.hour == 2
    assert end-start == timedelta(hours=24)
    earlier = {'start':'2021-02-01T01:01:00+00:00','id':'past','resolved_noaa':12804}
    later = {'start':'2021-02-02T01:59:00+00:00','id':'new','resolved_noaa':12804}
    verify = Supervisor([earlier,later],ContinuousCoverage())
    assert relabel(eligible,verify,False)['positive_event_ids'] == ['past']
    shifted = relabel(eligible,verify,True)
    assert shifted['positive_event_ids'] == ['new']
    assert not shifted['input_availability_at_issue_verified']
    with pytest.raises(ValueError,match='冻结训练或开发'):
        relabel({**eligible,'split':'test'},verify,True)
