from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from mdva.training.forecast_disk_xrs import history
from mdva.training.forecast_xrs_issue_refresh import paired_histories


def prepared():
    observe = datetime(2021, 1, 3, 12, tzinfo=timezone.utc)
    issue = observe + timedelta(minutes=70)
    times = np.arange(observe.timestamp() - 1500 * 60, issue.timestamp() + 60, 60.)
    values = np.full_like(times, 1e-7)
    values[times == observe.timestamp() + 60] = 4e-6
    values[times >= issue.timestamp() - 14 * 60] = 8e-4
    source = {15: (times, values), 16: (times, np.full_like(times, 8e-4))}
    frozen = history(observe.isoformat(), {15: source[15]})
    row = {'observed_utc': observe.isoformat(),
           'features': frozen['features'],
           'xrs': {k:v for k,v in frozen.items() if k != 'features'}}
    return row, issue, source


def test_refresh_preserves_satellite_and_excludes_minutes_after_margin():
    row, issue, sources = prepared()
    original, updated = paired_histories(row, issue, sources)
    assert updated['satellite'] == original['satellite'] == 15
    assert updated['features']['log_max_6h'] == pytest.approx(np.log10(4e-6))
    assert original['features']['log_max_6h'] == -7.
    assert updated['cutoff_utc'] == (issue - timedelta(minutes=15)).isoformat()
    assert updated['last_minute_end_utc'] == updated['cutoff_utc']


def test_refresh_does_not_switch_satellite_to_fill_missing_recent_bins():
    row, issue, sources = prepared()
    times, values = sources[15]
    keep = times < issue.timestamp() - 50 * 60
    sources[15] = (times[keep], values[keep])
    assert paired_histories(row, issue, sources)[1] is None
