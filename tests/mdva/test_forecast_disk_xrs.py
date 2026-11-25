from datetime import datetime, timezone
import json

import numpy as np
import pytest

pytest.importorskip('torch')

from mdva.training.forecast_disk_xrs import history


def test_history_uses_only_completed_minutes_before_warning_margin():
    observed = datetime(2021, 6, 12, 12, 0, 23, tzinfo=timezone.utc)
    cutoff = int(observed.timestamp() // 60) * 60 - 15*60
    times = np.arange(cutoff - 1440*60, cutoff + 180, 60)
    values = np.where(times < cutoff, 5e-8, 1e-3)
    result = history(observed.isoformat(), {16: (times, values)})
    assert result['last_minute_end_utc'] == result['cutoff_utc']
    assert result['valid_minutes_24h'] == 1440
    assert result['features']['log_max_24h'] == pytest.approx(np.log10(5e-8))
    assert result['features']['m_level_minute_share'] == 0
    json.dumps(result, allow_nan=False)


def test_history_does_not_fill_missing_or_stale_xrs():
    observed = datetime(2021, 6, 12, 12, 0, tzinfo=timezone.utc)
    cutoff = int(observed.timestamp()) - 15*60
    times = np.arange(cutoff - 1440*60, cutoff - 25*60, 60)
    assert history(observed.isoformat(), {16: (times, np.full(len(times), 5e-8))}) is None
