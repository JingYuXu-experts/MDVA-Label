from datetime import datetime, timedelta, timezone

from mdva.training.forecast_disk_supervision import label_window


def test_disk_label_respects_observation_cutoff_and_unknown_coverage():
    start = datetime(2021, 4, 1, 12, tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    def event(at, duplicate=False):
        return {'start': at.isoformat(), 'time_resolution_seconds': 60,
                'catalogue_duplicate_candidate': duplicate, 'resolved_noaa': None}

    assert label_window(start, end, [event(start - timedelta(minutes=1))], True)[0] == 0
    assert label_window(start, end, [], False)[0] is None
    assert label_window(start, end, [event(start)], True)[0] is None
    assert label_window(start, end, [event(end - timedelta(seconds=30))], True)[0] is None
    assert label_window(start, end, [event(start + timedelta(hours=2), True)], True)[0] is None
    assert label_window(start, end, [event(start + timedelta(hours=2))], False)[0] == 1
