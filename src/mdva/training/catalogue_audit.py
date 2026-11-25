"""Snapshot NOAA's science-quality composite catalogue without relabeling source data."""
from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path

from .datasets import read_frozen
from .runtime import atomic_json, digest, now

BASE = 'https://data.ngdc.noaa.gov/platforms/solar-space-observing-satellites/goes/multi/l2/data/xrsf-l2-flrpt_science/csv/'
README = 'https://data.ngdc.noaa.gov/platforms/solar-space-observing-satellites/goes/goes16/l2/docs/GOES_Flare_Report_ReadMe.pdf'


def timestamp(text):
    if not text:
        return None
    return datetime.fromisoformat(text.replace('Z', '+00:00')).replace(tzinfo=timezone.utc).isoformat()


def download(output, name):
    path = output/name
    if not path.exists():
        # Outbound traffic remains in the application's sole public-source collector.
        from ..web.space_weather import download_flare_catalogue
        data = download_flare_catalogue(name)
        if len(data) > 20*1024*1024:
            raise ValueError('权威目录超过预期大小，停止核查')
        temporary = path.with_suffix('.part')
        temporary.write_bytes(data)
        temporary.replace(path)
    return {'name': name, 'url': BASE+name, 'sha256': digest(path), 'bytes': path.stat().st_size}


def audit(dataset, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    files = [f'sci_xrsf-l2-flrpt_geo_y{y}_v1-0-1.csv' for y in range(2010, 2025)]
    files.append('sci_xrsf-l2-flrpt_geo_metadata.json')
    with ThreadPoolExecutor(max_workers=3) as pool:
        identities = list(pool.map(lambda name: download(output, name), files))
    local = [r for r in read_frozen(dataset, 'events.jsonl') if r.get('valid')]
    official, byday = [], defaultdict(list)
    for name in files[:-1]:
        for row in csv.DictReader((output/name).open(encoding='utf-8')):
            event = {'peak': timestamp(row['time']), 'start': timestamp(row['start_time']),
                     'end': timestamp(row['end_time']), 'class': row['flare_class'],
                     'is_m_plus': row['flare_class'].startswith(('M', 'X')),
                     'active_region_raw': row['active_region'], 'flare_id': row['flare_id'],
                     'irradiance_source': row['xrsb_irrad_source'], 'file': name,
                     'swpc_event_id': row['event_id_swpc'],
                     'longitude_swpc': row['flare_loc_swpc_hgs_lon'],
                     'latitude_swpc': row['flare_loc_swpc_hgs_lat'],
                     'peak_saturated': row['peak_saturated']}
            official.append(event)
            byday[event['start'][:10]].append(event)
    matches, missing = [], []
    for event in local:
        start = datetime.fromisoformat(event['start'])
        options = []
        # Adjacent-day peaks are rare but cannot be silently discarded at midnight.
        from datetime import timedelta
        for shift in (-1, 0, 1):
            for candidate in byday[(start+timedelta(days=shift)).date().isoformat()]:
                ds = abs((datetime.fromisoformat(candidate['start'])-start).total_seconds())
                dp = abs((datetime.fromisoformat(candidate['peak'])-datetime.fromisoformat(event.get('peak') or event['start'])).total_seconds())
                if min(ds, dp) > 240:
                    continue
                raw = candidate['active_region_raw']
                same_region = bool(raw and event.get('noaa') and int(float(raw)) % 10000 == event['noaa'] % 10000)
                options.append((not same_region, min(ds, dp), candidate, same_region))
        options.sort(key=lambda x: (x[0], x[1]))
        if not options or (len(options) > 1 and options[0][:2] == options[1][:2]):
            missing.append({'excel_row': event['excel_row'], 'start': event['start'],
                            'class': event['raw_class'], 'reason': 'no_unique_time_match'})
            continue
        _, offset, candidate, same_region = options[0]
        matches.append({'excel_row': event['excel_row'], 'local_start': event['start'],
                        'local_class': event['raw_class'], 'noaa': event.get('noaa'),
                        'official_flare_id': candidate['flare_id'], 'official_class': candidate['class'],
                        'same_reported_region': same_region, 'time_offset_seconds': offset,
                        'M_threshold_agrees': event['is_m_plus'] == candidate['is_m_plus']})
    years = {}
    for year in range(2010, 2025):
        a = [r for r in local if r['start'].startswith(str(year))]
        b = [r for r in official if r['start'].startswith(str(year))]
        years[str(year)] = {'reference_events': len(a), 'reference_M_plus': sum(r['is_m_plus'] for r in a),
                            'official_events': len(b), 'official_M_plus': sum(r['is_m_plus'] for r in b)}
    report = {'created_utc': now(), 'source': BASE, 'readme': README, 'snapshots': identities,
              'events_by_year': years, 'reference_events': len(local), 'official_events': len(official),
              'unique_time_matches': len(matches), 'M_threshold_disagreements': sum(not r['M_threshold_agrees'] for r in matches),
              'time_matches_without_matching_region': sum(not r['same_reported_region'] for r in matches),
              'known_20201129': [r for r in official if r['start'].startswith('2020-11-29') and r['is_m_plus']],
              'reference_catalogue_unchanged': True,
              'conclusion': '存在跨产品等级及事件覆盖差异，原目录只用于标注清楚的参考任务复现。新权威监督需新清单。',
              'remaining_gates': ['读取XRS跨代校准及flare报告已知限制，确定统一等级口径',
                  '正式权威AR关联需处理编号、位置、峰时/起始时差及重复卫星事件',
                  '负例需XRS有效观测覆盖证明；事件目录自身不能证明全天无观测空档',
                  '历史发布时间未核验，不允许宣布业务实时预测可用'],
              'global_forecast_state': 'blocked', 'AR_operational_release_state': 'blocked'}
    atomic_json(output/'official-events.json', official)
    atomic_json(output/'reference-matches.json', matches)
    atomic_json(output/'unmatched-reference-events.json', missing)
    atomic_json(output/'audit.json', report)
    return report


if __name__ == '__main__':
    from .__main__ import DEFAULT_DATA, DEFAULT_EXPERIMENT
    report = audit(DEFAULT_DATA, DEFAULT_EXPERIMENT/'official-catalogue')
    print(json.dumps({k: report[k] for k in ('events_by_year', 'unique_time_matches', 'M_threshold_disagreements')}, ensure_ascii=False, indent=2))
