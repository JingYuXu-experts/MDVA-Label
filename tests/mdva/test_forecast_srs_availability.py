from datetime import date

import pytest

from mdva.training.forecast_srs_availability import classify, lookup_day, parse_srs, utc


REPORT = b""":Issued: 2022 Jan 01 0030 UTC
Joint USAF/NOAA Solar Region Summary
I. Regions with Sunspots. Locations Valid at 31/2400Z
2916 N12W22 001 0220 Dkc 09 09 Beta
IA. H-alpha Plages without Spots. Locations Valid at 31/2400Z
None
II. Regions Due to Return
2917 S05 000
"""


def test_end_of_year_midnight_and_visibility_sections():
    report = parse_srs(REPORT, date(2022, 1, 1))
    assert report['visible_regions'][0]['location_valid_utc'] == '2022-01-01T00:00:00+00:00'
    instant = utc('2022-01-01T01:00:00+00:00')
    state, position = classify(report, {'noaa': 12916}, instant)
    assert state == 'unique_visible_number_in_prior_report' and position['longitude_west_deg'] == 22
    assert classify(report, {'noaa': 12917}, instant) == ('no_visible_number_in_prior_report', None)


def test_future_report_and_corrected_archive_are_not_eligible():
    future = utc('2022-01-01T00:29:59+00:00')
    assert lookup_day(future) == date(2021, 12, 31)
    report = parse_srs(REPORT, date(2022, 1, 1))
    assert classify(report, {'noaa': 12916}, future) == ('report_later_than_observation', None)
    corrected = parse_srs(b'# Corrected copy\n' + REPORT, date(2022, 1, 1))
    assert classify(corrected, {'noaa': 12916}, utc('2022-01-01T01:00:00+00:00')) == (
        'correction_availability_unverified', None
    )
    with pytest.raises(ValueError):
        parse_srs(REPORT, date(2021, 12, 31))
