from datetime import datetime, timezone

import numpy as np

from mdva.training.ar_quiet_weak_field_controls import signed_support_filter
from mdva.training.ar_quiet_window_audit import classify_pair, explicit_empty, physical_stats


RAW = b'''I. Regions with Sunspots
None
IA. H-alpha Plages without Spots
None
II. Regions Due to Return
2719
'''


def receipt(issued, raw=RAW, corrected=False, visible=()):
    return {'state':'ready','raw':raw,'report':{'issued_utc':issued,
            'corrected_marker':corrected,'visible_regions':list(visible)}}


def test_quiet_requires_both_sections_explicitly_empty():
    when = '2019-01-01T00:30:00+00:00'
    assert explicit_empty(receipt(when))
    assert not explicit_empty(receipt(when,RAW.replace(b'None',b'',1)))
    assert not explicit_empty(receipt(when,RAW.replace(b'None',b'No data\nNone',1)))
    assert not explicit_empty(receipt(when,corrected=True))
    assert not explicit_empty(receipt(when,visible=[{'short_noaa':2742}]))


def test_reports_must_bracket_observation_and_have_no_gaps():
    instant = datetime(2019,1,1,23,59,tzinfo=timezone.utc)
    before = receipt('2019-01-01T00:30:00+00:00')
    after = receipt('2019-01-02T00:30:00+00:00')
    assert classify_pair(instant,before,after)=='srs_quiet_window'
    assert classify_pair(instant,before,{'state':'archive_unavailable'})=='reports_unavailable'
    assert classify_pair(instant,before,before)=='report_timing_mismatch'
    assert classify_pair(instant,receipt('2018-12-31T00:30:00+00:00'),after)=='report_timing_mismatch'


def test_signed_support_counts_only_valid_pixels_from_each_region():
    field = np.full((1024,1024),np.nan,dtype=np.float32)
    mask = np.zeros_like(field,dtype=bool)
    mask[500:504,500:508] = True
    field[500:504,500:504] = 105.
    field[500:504,504:508] = -105.
    mask[510:514,510:514] = True
    field[510:514,510:514] = -300.
    mask[:4,:8] = True
    field[:4,:4],field[:4,4:8] = 120.,-120.
    stats = physical_stats(mask,field)
    assert [c['field_pixels'] for c in stats]==[0,32,16]
    assert [c['both_signs_supported'] for c in stats]==[False,True,False]
    kept = signed_support_filter(mask,stats)
    assert kept.sum()==32 and kept[500:504,500:508].all()
