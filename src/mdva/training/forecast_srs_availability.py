"""Audit whether an independent, timely SRS contains retrospective AR identifiers."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen

import numpy as np

from .durable import atomic_json, commit
from .journal import read
from .runtime import configure_environment, digest, now, output_path


ARCHIVE = ('https://www.ngdc.noaa.gov/stp/space-weather/swpc-products/'
           'daily_reports/solar_region_summaries')
ISSUED = re.compile(r'^:Issued:\s+(\d{4} [A-Za-z]{3} \d{2} \d{4}) UTC\s*$', re.M)
POSITION = re.compile(r'^(\d{4})\s+([NS])(\d{2})([EW])(\d{2})\s+')
VALID = re.compile(r'Locations Valid at (\d{2})/(\d{4})Z', re.I)
MONTHS = dict(zip('Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split(), range(1, 13)))


def utc(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError('观测必须有时区')
    return stamp.astimezone(timezone.utc)


def parse_srs(raw, expected_day):
    text = raw.decode('ascii').replace('\r\n', '\n')
    matches = ISSUED.findall(text)
    if len(matches) != 1 or 'Joint USAF/NOAA Solar Region Summary' not in text:
        raise ValueError('缺少唯一SRS发布时间或产品标题')
    year, month, day, clock = matches[0].split()
    issued = datetime(int(year), MONTHS[month], int(day), int(clock[:2]), int(clock[2:]), tzinfo=timezone.utc)
    if issued.date() != expected_day:
        raise ValueError('归档日期和内文发布时间不同')
    corrected = bool(re.search(r'^[:#].*\b(?:CORRECTED|REISSUED)\b', text, re.I | re.M))
    active = []
    section = None
    section_time = None
    sections_seen = set()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(('I.', 'IA.')):
            section = line.split('.', 1)[0]
            if section in sections_seen:
                raise ValueError('活动区分节重复')
            sections_seen.add(section)
            timing = VALID.search(line)
            if not timing:
                raise ValueError('活动区分节缺少有效时刻')
            candidates = [issued.date() - timedelta(days=1), issued.date()]
            valid_day = next((day for day in candidates if day.day == int(timing[1])), None)
            if valid_day is None:
                raise ValueError('活动区位置日期无法核对')
            hhmm = timing[2]
            start = datetime.combine(valid_day, time(0), timezone.utc)
            section_time = start + timedelta(hours=int(hhmm[:2]), minutes=int(hhmm[2:]))
            if section_time > issued or int(hhmm[2:]) >= 60 or int(hhmm[:2]) > 24:
                raise ValueError('位置时刻无效或晚于发布时间')
            continue
        if line.startswith('II.'):
            break
        if section is None or line == 'None' or not line[:1].isdigit():
            continue
        match = POSITION.match(line)
        if not match or int(match[3]) > 90 or int(match[5]) > 90:
            raise ValueError('可见活动区位置格式无法核对')
        number, ns, lat, ew, lon = match.groups()
        active.append({
            'short_noaa': int(number), 'section': section,
            'latitude_north_deg': int(lat) * (-1 if ns == 'S' else 1),
            'longitude_west_deg': int(lon) * (-1 if ew == 'E' else 1),
            'location_valid_utc': section_time.isoformat(),
        })
    if sections_seen != {'I', 'IA'}:
        raise ValueError('SRS活动区报告分节缺失')
    return {'issued_utc': issued.isoformat(), 'corrected_marker': corrected, 'visible_regions': active}


def lookup_day(observation):
    # NOAA publishes the daily report at 00:30; the actual header is checked too.
    return observation.date() - timedelta(days=int(observation.time() < time(0, 30)))


def classify(report, sample, observation):
    issued = utc(report['issued_utc'])
    if issued > observation:
        return 'report_later_than_observation', None
    if report['corrected_marker']:
        return 'correction_availability_unverified', None
    options = [region for region in report['visible_regions'] if region['short_noaa'] == sample['noaa'] % 10000]
    if not options:
        return 'no_visible_number_in_prior_report', None
    if len(options) != 1:
        return 'ambiguous_visible_number', None
    return 'unique_visible_number_in_prior_report', options[0]


def fetch(day, cache):
    filename = day.strftime('%Y%m%dSRS.txt')
    url = f'{ARCHIVE}/{day:%Y/%m}/{filename}'
    path = cache / filename
    meta = path.with_suffix('.source.json')
    if path.exists():
        if digest(path) != read(meta, {}).get('sha256'):
            raise ValueError('归档缓存指纹缺失或变化')
        raw = path.read_bytes()
    else:
        with urlopen(Request(url, headers={'User-Agent': 'MDVA local research SRS audit'}), timeout=10) as response:
            if response.geturl() != url:
                raise ValueError('SRS归档重定向到未核验来源')
            raw = response.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise ValueError('SRS归档异常大小')
        parse_srs(raw, day)
        sha = hashlib.sha256(raw).hexdigest()
        commit(path, lambda stream: stream.write(raw))
        atomic_json(meta, {'url': url, 'sha256': sha, 'download_utc': now(), 'bytes': len(raw)})
    return parse_srs(raw, day), digest(path)


def run(dataset, joined, output):
    dataset, joined = Path(dataset).resolve(), Path(joined).resolve()
    output = output_path(output, dataset)
    if output.is_relative_to(joined) or (output / 'summary.json').exists():
        raise ValueError('审计结果保存在独立版本')
    fingerprint = digest(joined / 'development-joins.jsonl')
    source = read(joined / 'summary.json')
    if source['join_rows_sha256'] != fingerprint or source['frozen_manifest_sha256'] != digest(dataset / 'manifest-hashes.json'):
        raise ValueError('冻结历史关联身份不符')
    with (joined / 'development-joins.jsonl').open(encoding='utf-8') as stream:
        items = [json.loads(line) for line in stream]
    eligible = sorted(
        [row for row in items if row['join_state'] == 'exact_reference_join'
         and row['split'] == row['detector_split'] == 'validation'],
        key=lambda row: row['sample_id'],
    )
    positive = [row for row in eligible if row['target'] == 1]
    negative = [row for row in eligible if row['target'] == 0]
    if len(positive) != 13 or len(negative) != 722:
        raise ValueError('历史验证样本数量与核验版本不同')
    indices = sorted(np.random.default_rng(2026).choice(len(negative), size=26, replace=False))
    cohort = positive + [negative[index] for index in indices]
    plan = {
        'archive': ARCHIVE, 'cohort': [row['sample_id'] for row in cohort],
        'join_rows_sha256': fingerprint, 'code_sha256': digest(__file__),
        'selection': 'Pilot: complete13 positive development windows and26 fixed randomly chosen negative windows.',
        'scope': 'Audit report availability using retrospective NOAA identity for scoring only.',
    }
    output.mkdir(parents=True, exist_ok=True)
    protocol = output / 'protocol.json'
    if protocol.exists() and read(protocol) != plan:
        raise ValueError('不可混用审计版本')
    if not protocol.exists():
        atomic_json(protocol, plan)

    reports = {}
    results = []
    for sample in cohort:
        observation = utc(sample['time_utc'])
        day = lookup_day(observation)
        if day not in reports:
            try:
                report, sha = fetch(day, output / 'archive')
                reports[day] = report, sha, None
            except (OSError, ValueError, KeyError) as exc:
                reports[day] = None, None, str(exc)
        report, sha, error = reports[day]
        state, region = classify(report, sample, observation) if report else ('report_unavailable', None)
        results.append({
            'sample_id': sample['sample_id'], 'target': sample['target'], 'reference_noaa': sample['noaa'],
            'observed_utc': observation.isoformat(), 'requested_archive_day': day.isoformat(),
            'report_issued_utc': report['issued_utc'] if report else None,
            'archive_sha256': sha, 'state': state, 'region': region, 'error': error,
        })
        atomic_json(output / 'status.json', {'state': 'auditing', 'done': len(results), 'total': len(cohort)})

    atomic_json(output / 'rows.json', results)
    summary = {
        'state': 'completed', 'created_utc': now(), 'samples': len(results), 'archive_days': len(reports),
        'source_available_days': sum(value[0] is not None for value in reports.values()),
        'by_target': {str(target): dict(Counter(r['state'] for r in results if r['target'] == target))
                      for target in (1, 0)},
        'archive_scope': 'Header-time-eligible historical archive. First dissemination or later unmarked edits are not independently proven.',
        'identity_scope': 'Historical reference number is used to score coverage. No detector assignment or forecast is produced.',
        'protocol_sha256': digest(protocol), 'rows_sha256': digest(output / 'rows.json'),
        'test_accessed': False, 'operational_identity_verified': False,
    }
    atomic_json(output / 'summary.json', summary)
    atomic_json(output / 'status.json', {'state': 'completed', 'done': len(results), 'total': len(cohort)})
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('dataset', 'joined', 'output'):
        parser.add_argument('--' + key, required=True)
    args = parser.parse_args()
    configure_environment()
    result = run(**vars(args))
    print(json.dumps({key: result[key] for key in ('state', 'archive_days', 'source_available_days', 'by_target')}))


if __name__ == '__main__':
    main()
