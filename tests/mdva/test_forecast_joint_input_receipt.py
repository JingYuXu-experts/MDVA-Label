from datetime import datetime, timedelta, timezone

from mdva.training.forecast_joint_input_receipt import join


def test_joint_input_uses_hmi_anchor_but_starts_outcome_window_after_all_receipts():
    observed = datetime(2021, 3, 12, 13, 30, tzinfo=timezone.utc)
    later = observed + timedelta(minutes=60)
    hmi = {
        'observation_utc': observed.isoformat(), 'fits_received_utc': later.isoformat(),
        'quality': 1024, 'state': 'quality_review_required', 'fits_sha256': 'example',
    }
    payload = [
        {'energy': '0.1-0.8nm', 'time_tag': (observed + timedelta(minutes=i)).isoformat(), 'satellite': 16,
         'flux': 1e-6 if i < -15 else 1e-3, 'electron_contamination': False}
        for i in range(-1455, 55)
    ]
    xrs = {'feeds': {'primary': {
        'state': 'received', 'received_utc': (later + timedelta(seconds=10)).isoformat(), 'raw_sha256': 'example',
    }}}

    record = join(hmi, 'hash', {'primary': payload}, xrs)

    saved = record['feeds']['primary']['hmi_observation_anchored_features']
    assert saved['last_minute_end_utc'] == (observed - timedelta(minutes=15)).isoformat()
    assert saved['features']['log_max_24h'] == -6.
    assert record['prospective_24h_outcome_can_be_checked_after_utc'] == (
        later + timedelta(hours=24, seconds=10)
    ).isoformat()
    assert 'HMI_quality_review_required' in record['remaining_gates'] and not record['issued_forecast']
