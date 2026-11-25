from datetime import datetime, timedelta, timezone

from mdva.training.forecast_prospective_outcome import assess


T = datetime(2026, 11, 19, 5, 8, tzinfo=timezone.utc)


def observed(minute, *, contaminated=False, flux=1e-6):
    return {'time_tag': minute.isoformat(), 'satellite': 18, 'energy': '0.1-0.8nm',
            'flux': flux, 'electron_contamination': contaminated}


def listed(begin, size):
    return {'begin_time': begin.isoformat(), 'max_class': size,
            'max_time': (begin + timedelta(minutes=1)).isoformat(),
            'end_time': (begin + timedelta(minutes=2)).isoformat(), 'satellite': 18}


def test_missing_or_contaminated_minute_cannot_certify_quiet_window():
    start = T + timedelta(seconds=30)
    end = T + timedelta(minutes=2, seconds=30)
    rows = [observed(T), observed(T + timedelta(minutes=1), contaminated=True), observed(T + timedelta(minutes=2))]
    feeds = {'primary': (rows, end + timedelta(minutes=2))}
    catalog = [listed(T - timedelta(minutes=15), 'C2.3')]

    assert assess(start, end, feeds, catalog)['state'] == 'outcome_inconclusive'
    feeds['secondary'] = ([observed(T + timedelta(minutes=1))], end + timedelta(minutes=2))
    result = assess(start, end, feeds, catalog)
    assert result['state'] == 'no_m_plus_seen_in_provisional_products'
    assert result['supported_minutes'] == result['expected_minutes'] == 3


def test_event_starting_in_the_cutoff_minute_does_not_count_as_future():
    start = T + timedelta(seconds=30)
    end = start + timedelta(hours=24)
    result = assess(start, end, {}, [listed(T, 'M1.4')])
    assert result['state'] == 'outcome_inconclusive'
    assert result['boundary_events'] == [T.isoformat()]

    later = T + timedelta(minutes=1)
    result = assess(start, end, {}, [listed(later, 'M1.0')])
    assert result['state'] == 'm_plus_listed_by_goes'
    assert result['m_plus_events'][0]['begin_utc'] == later.isoformat()
