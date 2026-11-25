"""Frozen, resumable historical XRS collection; CPU/network only, never training."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import timedelta
import json
from pathlib import Path
import shutil
import time
from urllib.error import HTTPError, URLError

from .datasets import read_frozen
from .durable import atomic_json
from .runtime import digest, exclusive, now, output_path
from .xrs_coverage import utc, read_source, snapshot_year

LEGACY = 'https://www.ncei.noaa.gov/data/goes-space-environment-monitor/access/science/xrs/GOES_1-15_XRS_Science-Quality_Data_Readme.pdf'
MODERN = 'https://data.ngdc.noaa.gov/platforms/solar-space-observing-satellites/goes/goes16/l1b/docs/GOES-R_EXIS_XRS_L1b_Data_ReadMe.pdf'
# Discovery bounds, NOT claims of uninterrupted measurements or operational role.
# Legacy endpoints have only month precision here; file headers give exact dates.
AVAILABILITY = [
    (13, '2013-06-01', '2018-01-01', LEGACY, 'science-quality start month; earlier anomalous-mode data excluded'),
    (14, '2009-09-01', '2020-04-01', LEGACY, 'month-precision bounds'),
    (15, '2010-04-01', '2020-04-01', LEGACY, 'month-precision bounds'),
    (16, '2017-02-07', '2025-04-08', MODERN, 'published date bounds'),
    (17, '2018-06-01', '2023-01-11', MODERN, 'published date bounds'),
    (18, '2022-06-17', '2025-01-01', MODERN, 'published start, scope capped at 2024, not end of mission'),
]


def window_days(start, end):
    start, end = utc(start), utc(end)
    if end-start != timedelta(hours=24):
        raise ValueError('仅采集已定义的24小时监督窗口')
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    result = []
    while day < end:
        result.append(day.date().isoformat())
        day += timedelta(days=1)
    return result


def code_identity():
    paths = [Path(__file__), Path(__file__).with_name('xrs_coverage.py'),
             Path(__file__).parents[1]/'web/space_weather.py']
    return {p.name: digest(p) for p in paths}


def plan_rows(rows):
    days = defaultdict(set)
    for row in rows:
        if not row.get('eligible'):
            continue
        if utc(row['time_utc']) != utc(row['target_start']):
            raise ValueError('输入时刻和监督起始不一致')
        identity = (row['time_utc'], row['noaa'], row['split'])
        for day in window_days(row['target_start'], row['target_end']):
            days[day].add(identity)
    day_rows = [{'day': day, 'unique_AR_windows': len(members),
                 'splits': dict(Counter(member[2] for member in members))}
                for day, members in sorted(days.items())]
    jobs = []
    for satellite, first, last, source, note in AVAILABILITY:
        matching = [d for d in day_rows if first <= d['day'] < last]
        years = defaultdict(list)
        for day in matching:
            years[int(day['day'][:4])].append(day['day'])
        for year, needed in years.items():
            jobs.append({'id': f'g{satellite}-y{year}', 'satellite': satellite,
                         'year': year, 'needed_days': needed, 'availability_source': source,
                         'availability_basis': note, 'role': 'quality coverage candidate, not operational primary'})
    return day_rows, sorted(jobs, key=lambda j: (j['year'], j['satellite']))


def create_plan(dataset, destination):
    dataset = Path(dataset)
    destination = output_path(destination, dataset)
    if destination.exists():
        raise ValueError('采集计划版本已存在，禁止覆盖')
    days, jobs = plan_rows(read_frozen(dataset, 'forecast-ready.jsonl'))
    report = {'schema': 1, 'created_utc': now(), 'dataset': str(dataset.resolve()),
              'dataset_manifest_sha256': digest(dataset/'manifest-hashes.json'),
              'code': code_identity(), 'scope': 'quality minute supervision audit only',
              'day_count': len(days), 'days': days, 'jobs': jobs,
              'annual_files': len(jobs), 'max_download_bytes_per_file': 64*1024*1024,
              'selection': 'all listed science candidates overlapping needed days; no label/score ranking',
              'not_covered_by_schedule': [r['day'] for r in days if not any(
                  first <= r['day'] < last for _, first, last, _, _ in AVAILABILITY)],
              'future_training_started': False}
    atomic_json(destination, report)
    return report


def acquire_one(job, sources, attempts=3):
    for attempt in range(1, attempts+1):
        try:
            meta = snapshot_year(sources, job['satellite'], job['year'])
            quality = read_source(sources, meta)
            return {'state': 'verified', 'attempts': attempt, 'filename': meta['filename'],
                    'sha256': meta['sha256'], 'bytes': meta['bytes'], 'valid_minutes': quality['valid_minutes'],
                    'source_metadata_sha256': digest((Path(sources)/meta['filename']).with_suffix('.source.json')),
                    'records': quality['records'], 'declared_start': quality['time_coverage_start'],
                    'declared_end': quality['time_coverage_end'], 'finished_utc': now()}
        except (HTTPError, FileNotFoundError) as exc:
            if isinstance(exc, FileNotFoundError) or exc.code in (404, 410):
                return {'state': 'unavailable', 'attempts': attempt, 'error': str(exc), 'finished_utc': now()}
            if attempt == attempts:
                return {'state': 'failed', 'attempts': attempt, 'error': str(exc), 'finished_utc': now()}
        except (URLError, TimeoutError, ConnectionError, OSError) as exc:
            if attempt == attempts:
                return {'state': 'failed', 'attempts': attempt, 'error': str(exc), 'finished_utc': now()}
        except (ValueError, KeyError) as exc:
            return {'state': 'invalid_product', 'attempts': attempt, 'error': str(exc), 'finished_utc': now()}
        time.sleep(min(2**attempt, 8))
    raise AssertionError('unreachable retry state')


def collect(plan_path, sources, output, *, workers=2, limit=None, retry_unavailable=False):
    if workers not in (1, 2):
        raise ValueError('采集并发仅允许1或2，不占GPU')
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError('采集数量上限无效')
    plan_path, sources, output = map(Path, (plan_path, sources, output))
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    dataset = Path(plan['dataset'])
    output, sources = output_path(output, dataset), output_path(sources, dataset)
    if output == sources or output.is_relative_to(sources) or sources.is_relative_to(output):
        raise ValueError('采集状态与原始缓存目录必须分开')
    if digest(dataset/'manifest-hashes.json') != plan['dataset_manifest_sha256'] or plan['code'] != code_identity():
        raise ValueError('数据或采集代码身份改变，需建立新计划版本；已核验原始缓存可复用')
    if shutil.disk_usage(output.parent).free < 2*1024**3:
        raise ValueError('剩余磁盘不足2GiB，暂不下载')
    output.mkdir(parents=True, exist_ok=True)
    with exclusive(output/'collector.lock'):
        state_path = output/'status.json'
        previous = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
        if previous and previous['plan_sha256'] != digest(plan_path):
            raise ValueError('状态目录属于不同采集计划')
        state = {'state': 'running', 'created_utc': previous.get('created_utc', now()),
                 'updated_utc': now(), 'plan_sha256': digest(plan_path),
                 'plan': str(plan_path.resolve()), 'sources': str(sources.resolve()),
                 'total': len(plan['jobs']), 'jobs': previous.get('jobs', {}),
                 'gpu_used': False, 'model_training': False}
        # A verified ledger is not sufficient: validate cached bytes on resume.
        for job in plan['jobs']:
            old = state['jobs'].get(job['id'], {})
            if old.get('state') == 'verified':
                path = (sources/old['filename']).resolve()
                metadata = path.with_suffix('.source.json')
                if (not path.is_relative_to(sources.resolve()) or not path.is_file() or
                        digest(path) != old['sha256'] or not metadata.is_file() or
                        digest(metadata) != old.get('source_metadata_sha256')):
                    raise ValueError('已完成年度缓存被修改或丢失，停止而非自动覆盖')
        pending = [j for j in plan['jobs'] if state['jobs'].get(j['id'], {}).get('state') != 'verified'
                   and (retry_unavailable or state['jobs'].get(j['id'], {}).get('state') != 'unavailable')]
        if limit:
            pending = pending[:limit]
        def save():
            state['updated_utc'] = now()
            state['counts'] = dict(Counter(r['state'] for r in state['jobs'].values()))
            atomic_json(state_path, state)
        active, cursor = {}, 0
        save()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while active or cursor < len(pending):
                paused = (output/'PAUSE').exists()
                while not paused and len(active) < workers and cursor < len(pending):
                    job = pending[cursor];cursor += 1
                    state['jobs'][job['id']] = {'state': 'running', 'started_utc': now()}
                    active[pool.submit(acquire_one, job, sources)] = job
                    save()
                if not active:
                    break
                done, _ = wait(active, timeout=5, return_when=FIRST_COMPLETED)
                for future in done:
                    job = active.pop(future)
                    state['jobs'][job['id']] = future.result()
                    print(json.dumps({'job': job['id'], **state['jobs'][job['id']]}, ensure_ascii=False), flush=True)
                save()
        states = {r['state'] for r in state['jobs'].values()}
        state['state'] = ('paused' if (output/'PAUSE').exists() else
                          'completed' if len(state['jobs']) == state['total'] and states == {'verified'} else
                          'partial')
        save()
        return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prepare = sub.add_parser('plan')
    prepare.add_argument('--dataset', required=True)
    prepare.add_argument('--output', required=True)
    run = sub.add_parser('collect')
    run.add_argument('--plan', required=True)
    run.add_argument('--sources', required=True)
    run.add_argument('--output', required=True)
    run.add_argument('--workers', type=int, default=2)
    run.add_argument('--limit', type=int)
    run.add_argument('--retry-unavailable', action='store_true')
    args = parser.parse_args()
    from .runtime import configure_environment
    configure_environment()
    if args.action == 'plan':
        result = create_plan(args.dataset, args.output)
        print(json.dumps({k: result[k] for k in ('day_count', 'annual_files', 'not_covered_by_schedule')}, ensure_ascii=False))
    else:
        result = collect(args.plan, args.sources, args.output, workers=args.workers,
                         limit=args.limit, retry_unavailable=args.retry_unavailable)
        print(json.dumps({k: result[k] for k in ('state', 'total', 'counts')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
