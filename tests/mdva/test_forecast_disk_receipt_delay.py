from datetime import datetime, timedelta, timezone

from mdva.training.forecast_disk_receipt_delay import reanchor


class Coverage:
    def __init__(self, complete):
        self.complete = complete

    def window(self, _start, _end):
        return {'complete': self.complete}


def test_receipt_based_window_excludes_early_event_and_includes_later_event():
    observed = datetime(2021, 1, 1, tzinfo=timezone.utc)
    row = {'observed_utc': observed.isoformat(), 'split': 'train', 'target': 1,
           'target_end_utc': (observed + timedelta(hours=24)).isoformat()}
    times = [observed + timedelta(minutes=15), observed + timedelta(days=1, minutes=35)]
    events = [{'id': str(i), 'start': stamp.isoformat(), 'time_resolution_seconds': 60,
               'catalogue_duplicate_candidate': False} for i, stamp in enumerate(times)]

    base = reanchor(row, 0, events, times, Coverage(True))
    delayed = reanchor(row, 70, events, times, Coverage(True))

    assert base['positive_event_ids'] == ['0']
    assert delayed['positive_event_ids'] == ['1']
    assert delayed['issue_utc'] == (observed + timedelta(minutes=70)).isoformat()
    assert reanchor(row, 70, events[:1], times[:1], Coverage(False))['target'] is None
