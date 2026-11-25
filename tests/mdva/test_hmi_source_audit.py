from datetime import datetime,timezone
import pytest
from mdva.training.hmi_source_audit import tai_utc


def test_tai_observation_is_not_mistaken_for_utc():
    assert tai_utc('2013.07.09_06:00:00_TAI')==datetime(2013,7,9,5,59,25,tzinfo=timezone.utc)
    assert tai_utc('2020.11.29_00:00:00_TAI')==datetime(2020,11,28,23,59,23,tzinfo=timezone.utc)
    assert tai_utc('2013.07.09_06:00:00.500_TAI').microsecond==500000


@pytest.mark.parametrize('value',['2013.07.09_06:00:00','2013-07-09T06:00:00Z','2013.07.09_06:00:00_UTC',None])
def test_unqualified_or_other_time_scales_are_not_silently_assumed_tai(value):
    with pytest.raises(ValueError,match='TAI'):tai_utc(value)
