"""Capture a prospective GOES XRS input and audit its readiness without issuing a forecast."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path

import numpy as np

from mdva.web.space_weather import SOURCES, fetch

from .durable import atomic_json, commit
from .forecast_disk_xrs import history
from .runtime import digest, output_path


PRIMARY = SOURCES['xray'][1].replace('1-day', '7-day')
FEEDS = {'primary': PRIMARY, 'secondary': PRIMARY.replace('/primary/', '/secondary/')}


def usable_minutes(payload, received):
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise ValueError('SWPC XRS分钟产品结构改变')
    stats = Counter()
    seen = set()
    groups = defaultdict(list)
    latest = None
    for row in payload:
        if row.get('energy') != '0.1-0.8nm':
            continue
        stats['long_band_rows'] += 1
        try:
            observed = datetime.fromisoformat(row['time_tag'].replace('Z', '+00:00'))
            satellite = row['satellite']
            flux = row['flux']
            flags = [row[key] for key in ('electron_contamination', 'electron_contaminaton') if key in row]
            if (observed.utcoffset() is None or observed.second or observed.microsecond
                    or type(satellite) is not int or satellite <= 0):
                raise ValueError('需带时区的整分钟和卫星编号')
        except (AttributeError, KeyError, TypeError, ValueError):
            stats['invalid_metadata'] += 1
            continue
        if observed.timestamp() + 60 > received.timestamp():
            stats['unended_minutes'] += 1
            continue
        key = satellite, observed
        if key in seen:
            raise ValueError('同一卫星有重复分钟，不能生成预报输入')
        seen.add(key)
        latest = observed if latest is None else max(latest, observed)
        if not flags or any(type(flag) is not bool for flag in flags) or any(flag != flags[0] for flag in flags):
            stats['quality_unknown'] += 1
            continue
        if flags[0]:
            stats['electron_contamination'] += 1
            continue
        if type(flux) not in (int, float) or not math.isfinite(flux) or flux <= 0:
            stats['invalid_flux'] += 1
            continue
        groups[satellite].append((observed.timestamp(), float(flux)))
    return {satellite: np.asarray(sorted(minutes)).T for satellite, minutes in groups.items()}, stats, latest


def audit(payload, received):
    series, stats, latest = usable_minutes(payload, received)
    metrics = history(received.isoformat(), series)
    return {'counts': dict(stats), 'usable_minutes_by_satellite': {k: len(v[0]) for k, v in series.items()},
            'newest_observation_utc': latest.isoformat() if latest else None,
            'newest_observation_age_seconds': (received - latest).total_seconds() if latest else None,
            'usable_operational_history': metrics, 'science_archive_equivalence_verified': False}


def run(dataset, output):
    output = output_path(output, Path(dataset).resolve())
    output.mkdir(parents=True, exist_ok=False)
    feeds = {}
    for name, url in FEEDS.items():
        started = datetime.now(timezone.utc)
        item = {'url': url, 'request_started_utc': started.isoformat()}
        try:
            raw, headers = fetch(url)
            received = datetime.now(timezone.utc)
            item['received_utc'] = received.isoformat()
            item['server_headers'] = {k.lower(): v for k, v in headers.items()
                                      if k.lower() in ('date', 'last-modified', 'age', 'etag')}
            path = output / f'{name}.json'
            commit(path, lambda file: file.write(raw))
            item['raw_sha256'] = digest(path)
            item.update(audit(json.loads(raw), received))
            item['state'] = 'received'
        except (OSError, ValueError) as exc:
            item.update(state='failed', error=str(exc))
        feeds[name] = item
    result = {'state': 'completed', 'feeds': feeds, 'issued_forecast': False,
              'code_sha256': digest(__file__),
              'earliest_supported_decision_time': 'local receipt timestamp per feed',
              'interpretation': 'Operational snapshot only; local receipt is an upper bound on availability, '
                                'and the public product cannot establish equivalence to reprocessed science quality.'}
    atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = run(args.dataset, args.output)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if any(item['state'] == 'received' for item in result['feeds'].values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
