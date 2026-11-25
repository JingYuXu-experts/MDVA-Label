from datetime import datetime,timedelta,timezone
import pytest
from mdva.training.ch_channel_pairing import filename_time,nearest


def test_nominal_clock_not_date_only_and_channel_mismatch_rejected():
    assert filename_time('AIA20190128_0600_0193.jpg','193')==datetime(2019,1,28,6,tzinfo=timezone.utc)
    with pytest.raises(ValueError):filename_time('AIA20190128_0600_0211.jpg','193')
    with pytest.raises(ValueError):filename_time('AIA20191328_0600_0193.jpg','193')


def test_nearest_preserves_future_clock_and_prefers_past_in_tie():
    t=datetime(2020,1,1,6,tzinfo=timezone.utc)
    past=t-timedelta(hours=6);future=t+timedelta(hours=6)
    assert nearest([past,future],t)==past
    assert nearest([future],t)>t
    assert nearest([past,t,future],t)==t
    assert nearest([],t) is None
