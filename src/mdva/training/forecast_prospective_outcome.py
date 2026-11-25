"""Audit the observed outcome of an elapsed HMI/GOES research input window."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

from .durable import atomic_json, commit
from .forecast_xrs_intake import FEEDS, usable_minutes
from .journal import read
from .runtime import digest, output_path
from .xrs_coverage import utc
from ..web.space_weather import fetch


EVENT_SOURCE = 'https://services.swpc.noaa.gov/json/goes/primary/xray-flares-7-day.json'
M_LEVEL = 1e-5
FLARE_CLASS = re.compile(r'([ABCMX])([0-9]+(?:\.[0-9]+)?)')


def window(receipt):
    start = utc(receipt['paired_available_no_earlier_than_utc'])
    end = utc(receipt['prospective_24h_outcome_can_be_checked_after_utc'])
    if (receipt['state'] != 'recorded_for_research' or receipt['issued_forecast']
            or end - start != timedelta(hours=24)):
        raise ValueError('需要已核验的联合输入留证和24小时窗口')
    return start, end


def assess(start, end, observations, payload):
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise ValueError('GOES耀斑清单格式不符')
    # Any flux minute overlapping either boundary is relevant to a conservative negative.
    minute = int(start.timestamp()) // 60 * 60
    required = set(range(minute, int(end.timestamp()) + 1, 60))
    supported = set()
    maxima = []
    for _, (minute_payload, received) in observations.items():
        series, _, _ = usable_minutes(minute_payload, received)
        for timestamps, values in series.values():
            for stamp, flux in zip(timestamps, values, strict=True):
                if stamp in required:
                    supported.add(stamp)
                    maxima.append(float(flux))
    in_window = []
    unresolved = []
    boundary = []
    previous_dates = set()
    for row in payload:
        begin = utc(row['begin_time'])
        previous_dates.add(begin.date())
        if begin > end:
            continue
        date_from = begin.replace(second=0, microsecond=0)
        if start >= date_from + timedelta(minutes=1):
            continue
        text = row.get('max_class')
        match = FLARE_CLASS.fullmatch(text) if isinstance(text, str) else None
        ended = row.get('end_time')
        if not match or not ended or not row.get('max_time'):
            unresolved.append(begin.isoformat())
            continue
        peak, finished = utc(row['max_time']), utc(ended)
        if not begin <= peak <= finished:
            raise ValueError('官方耀斑时序无效')
        code, size = match.groups()
        large = code == 'X' or (code == 'M' and float(size) >= 1)
        if large and begin <= start:
            boundary.append(begin.isoformat())
        elif large:
            in_window.append({'begin_utc': begin.isoformat(), 'peak_utc': peak.isoformat(),
                              'end_utc': finished.isoformat(), 'peak_class': text, 'satellite': row.get('satellite')})
    missing = sorted(required - supported)
    if in_window:
        state = 'm_plus_listed_by_goes'
    elif (not missing and not unresolved and not boundary and maxima and max(maxima) < M_LEVEL
          and any(d <= start.date() for d in previous_dates)):
        state = 'no_m_plus_seen_in_provisional_products'
    else:
        state = 'outcome_inconclusive'
    return {'state': state, 'm_plus_events': in_window, 'unresolved_events': unresolved, 'boundary_events': boundary,
            'missing_minutes_utc': [datetime.fromtimestamp(t, timezone.utc).isoformat() for t in missing],
            'expected_minutes': len(required), 'supported_minutes': len(supported),
            'max_long_band_flux_w_m2': max(maxima, default=None)}


def run(dataset, receipt_path, output):
    receipt = read(receipt_path)
    start, end = window(receipt)
    output = output_path(output, dataset)
    if output.exists():
        raise ValueError('预报回查结果已存在，使用新目录')
    # Wait for the final overlapping minute to end. A rolling feed may still lag.
    ready_after = end.replace(second=0, microsecond=0) + timedelta(minutes=1)
    result = {'receipt_sha256': digest(receipt_path), 'code_sha256': digest(__file__), 'start_utc': start.isoformat(),
              'end_utc': end.isoformat(), 'earliest_probe_utc': ready_after.isoformat(), 'issued_forecast': False,
              'scientific_confirmation': False, 'verification_kind': 'provisional operational GOES evidence'}
    if datetime.now(timezone.utc) < ready_after:
        return {**result, 'state': 'waiting_window_end'}
    observations = {}
    sources = {}
    for key, url in {**FEEDS, 'event_catalog': EVENT_SOURCE}.items():
        try:
            raw, _ = fetch(url)
            received = datetime.now(timezone.utc)
            payload = json.loads(raw)
            path = output / f'{key}.json'
            commit(path, lambda file: file.write(raw))
            sources[key] = {'url': url, 'received_utc': received.isoformat(), 'sha256': digest(path)}
            observations[key] = payload, received
        except (OSError, ValueError) as exc:
            sources[key] = {'url': url, 'error': str(exc)}
    catalog = observations.pop('event_catalog', None)
    evidence = {'state': 'outcome_inconclusive'}
    if catalog:
        try:
            evidence = assess(start, end, observations, catalog[0])
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            evidence['reason'] = f'上游记录无法可靠解释：{exc}'
    result.update(sources=sources, **evidence)
    atomic_json(output / 'summary.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('dataset', 'receipt-path', 'output'):
        parser.add_argument('--' + arg, required=True, type=Path)
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))
