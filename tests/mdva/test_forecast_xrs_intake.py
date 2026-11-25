from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

pytest.importorskip('torch')

from mdva.training.forecast_xrs_intake import audit
from mdva.training.forecast_xrs_product_compare import overlap


def rows(received, minutes):
    return [{'time_tag': (received - timedelta(minutes=minute)).isoformat(), 'satellite': 18,
             'energy': '0.1-0.8nm', 'flux': 4e-8, 'electron_contaminaton': False}
            for minute in minutes]


def test_operational_audit_enforces_receipt_time_and_flagged_gaps():
    received = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
    payload = rows(received, range(1460))
    payload[10]['flux'] = 1e-3
    payload[20]['electron_contaminaton'] = True
    del payload[21]['electron_contaminaton']
    result = audit(payload, received)
    assert result['counts']['unended_minutes'] == 1
    assert result['counts']['electron_contamination'] == 1
    assert result['counts']['quality_unknown'] == 1
    history = result['usable_operational_history']
    assert history['valid_minutes_24h'] == 1438
    assert history['features']['m_level_minute_share'] == 0
    assert result['science_archive_equivalence_verified'] is False


def test_operational_audit_rejects_duplicate_minutes_and_stale_series():
    received = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
    payload = rows(received, range(50, 1500))
    assert audit(payload, received)['usable_operational_history'] is None
    with pytest.raises(ValueError, match='重复分钟'):
        audit(payload + payload[:1], received)


def test_product_comparison_pairs_exact_minutes_and_reports_threshold_disagreements():
    operational = np.array([[0, 60, 120], [6e-6, 8e-6, 2e-5]])
    science = np.array([[60, 120, 180], [2e-5, 2e-5, 6e-6]])
    result = overlap(operational, science)
    assert result['matched_minutes'] == 2
    assert result['m_flux_disagreements'] == 1
    assert result['relative_abs_difference_p50'] == pytest.approx(.3)
