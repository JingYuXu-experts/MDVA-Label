"""Independent prospective-label preparation; no model fit or test scoring.

Old frozen targets remain untouched. Catalogue negatives require complete quality
minute coverage and absence of an unresolved M+ event in the target window.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import csv
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re

from .datasets import read_frozen
from .durable import atomic_json, commit
from .runtime import digest, now, output_path
from .xrs_coverage import load, utc


def official_events(directory):
    """Parse original, hash-verified CSVs instead of trusting a derived cache."""
    directory = Path(directory)
    audit = json.loads((directory/'audit.json').read_text(encoding='utf-8'))
    events, identities = [], []
    metadata_name = 'sci_xrsf-l2-flrpt_geo_metadata.json'
    metadata_identity = next((i for i in audit['snapshots'] if i['name'] == metadata_name), None)
    if not metadata_identity or digest(directory/metadata_name) != metadata_identity['sha256']:
        raise ValueError('缺少已核验的权威目录时间元数据')
    metadata = json.loads((directory/metadata_name).read_text(encoding='utf-8'))
    if 'Time [UTC]' not in metadata['variable_attributes']['time']['comments']:
        raise ValueError('权威目录未声明UTC，不能猜测时区')
    identities.append(metadata_identity)
    def catalogue_time(value):
        # This product explicitly declares UTC but its CSV omits an offset.
        if re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', value):
            value += '+00:00'
        return utc(value)
    for identity in audit['snapshots']:
        name = identity['name']
        if not re.fullmatch(r'sci_xrsf-l2-flrpt_geo_y20\d{2}_v\d+-\d+-\d+\.csv', name):
            continue
        path = directory/name
        if digest(path) != identity['sha256'] or path.stat().st_size != identity['bytes']:
            raise ValueError('权威事件目录指纹改变，不能沿用旧审计')
        identities.append(identity)
        with path.open(encoding='utf-8', newline='') as stream:
            for number, row in enumerate(csv.DictReader(stream), 2):
                match = re.fullmatch(r'([ABCMX])(\d+(?:\.\d+)?)', row['flare_class'])
                if not match:
                    raise ValueError('目录出现未知耀斑等级')
                if match[1] not in 'MX' or float(match[2]) < 1:
                    continue
                start, peak = (catalogue_time(row[k]) for k in ('start_time', 'time'))
                end = catalogue_time(row['end_time']) if row['end_time'] else None
                if start > peak or (end is not None and peak > end):
                    raise ValueError('权威目录事件时间不一致')
                events.append({'id': f'{name}:{number}:{row["flare_id"]}',
                               'flare_id': row['flare_id'], 'start': start.isoformat(),
                               'peak': peak.isoformat(), 'end': end.isoformat() if end else None,
                               'end_status': 'reported' if end else 'not_reported_not_required_by_onset_target',
                               'class': row['flare_class'], 'active_region_raw': row['active_region'],
                               'swpc_event_id': row['event_id_swpc'],
                               'irradiance_source': row['xrsb_irrad_source'],
                               'source_sha256': identity['sha256'], 'source_file': name,
                               'source_row': number, 'time_resolution_seconds': 60})
    if not identities or not events:
        raise ValueError('权威事件快照为空')
    return events, identities


def resolve_regions(events, rows):
    """Only unique nearby, explicitly full IDs can resolve four-digit SWPC IDs."""
    reported = defaultdict(list)
    for row in rows:
        noaa = row.get('noaa')
        if (type(noaa) is int and noaa >= 10000 and
                row.get('noaa_basis') == 'reported_full_id'):
            reported[noaa % 10000].append((utc(row['time_utc']), noaa))
    duplicate_ids = Counter(e.get('flare_id', e['id']) for e in events)
    duplicate_starts = Counter(e['start'] for e in events)
    result = []
    for event in events:
        raw = event['active_region_raw'].strip()
        candidates = []
        if re.fullmatch(r'\d+(?:\.0+)?', raw) and int(float(raw)) > 0:
            number = int(float(raw))
            start = utc(event['start'])
            candidates = sorted({noaa for time, noaa in reported[number % 10000]
                                 if abs(time-start) <= timedelta(days=14)
                                 and (number < 10000 or number == noaa)})
        duplicate = duplicate_ids[event.get('flare_id', event['id'])] > 1 or duplicate_starts[event['start']] > 1
        result.append({**event, 'resolved_noaa': candidates[0] if len(candidates) == 1 else None,
                       'catalogue_duplicate_candidate': duplicate,
                       'region_candidates': candidates,
                       'region_basis': ('unique_nearby_reported_full_id' if len(candidates) == 1
                                        else 'ambiguous' if candidates else 'unresolved')})
    return sorted(result, key=lambda e: (e['start'], e['id']))


class Supervisor:
    def __init__(self, events, coverage):
        self.events = sorted(events, key=lambda e: (e['start'], e['id']))
        self.starts = [utc(e['start']).timestamp() for e in self.events]
        self.coverage = coverage

    def sample(self, row):
        start, end = utc(row['target_start']), utc(row['target_end'])
        if utc(row['time_utc']) != start or end-start != timedelta(hours=24):
            raise ValueError('预测时刻与24小时目标窗不符')
        if type(row['noaa']) is not int or row['noaa'] < 10000:
            raise ValueError('样本缺少完整活动区身份')
        a, b = start.timestamp(), end.timestamp()
        candidates = self.events[bisect_right(self.starts, a-60):bisect_right(self.starts, b)]
        positives, uncertain, other = [], [], []
        for event in candidates:
            at = utc(event['start']).timestamp()
            # CSV reports minute precision. A boundary minute cannot determine
            # which side of an arbitrary second T the actual onset belonged to.
            within = at > a and at+60 <= b
            identity = event['resolved_noaa']
            if identity == row['noaa'] and within and not event.get('catalogue_duplicate_candidate'):
                positives.append(event['id'])
            elif event.get('catalogue_duplicate_candidate') or identity is None or identity == row['noaa']:
                uncertain.append({'id': event['id'],
                                  'reason': ('目录疑似重复事件待核验' if event.get('catalogue_duplicate_candidate') else
                                             'AR归属未确认' if identity is None else '起始分钟跨目标窗边界')})
            else:
                other.append(event['id'])
        coverage = self.coverage.window(row['target_start'], row['target_end'])
        if positives:
            target, state = 1, 'catalogue_positive'
        elif uncertain:
            target, state = None, 'unresolved_event_or_boundary'
        elif coverage['complete']:
            target, state = 0, 'catalogue_negative_quality_minutes_complete'
        else:
            target, state = None, 'incomplete_quality_minute_coverage'
        return {'sample_id': f'ARday:{row["time_utc"]}|NOAA:{row["noaa"]}',
                'split': row['split'], 'time_utc': row['time_utc'], 'noaa': row['noaa'],
                'target_start': row['target_start'], 'target_end': row['target_end'],
                'old_reference_target': row['target_m_plus'], 'official_target': target,
                'state': state, 'positive_events': positives, 'uncertain_events': uncertain,
                'other_reported_AR_events': other, 'coverage': coverage,
                'source_parameters': {'uri': row['parameters_uri'], 'line': row['parameter_line']},
                'source_feature_sha256': hashlib.sha256(json.dumps(row.get('features', {}),
                    sort_keys=True, allow_nan=False).encode('utf-8')).hexdigest(),
                'target_changed': target is not None and target != row['target_m_plus'],
                'input_availability_verified': False, 'usable_for_operational_training': False}


def report_text(summary, samples, events):
    names = {'catalogue_positive': '找到明确AR归属的M+事件',
             'catalogue_negative_quality_minutes_complete': '无对应事件且质量分钟覆盖完整',
             'unresolved_event_or_boundary': '事件归属或时间边界待核验',
             'incomplete_quality_minute_coverage': '尚缺完整质量分钟覆盖'}
    lines = ['# 预测监督审计：准备未完成', '',
             '本报告仅核查元数据、事件归属和标签依据；没有训练模型，没有计算任何测试成绩。', '',
             f'冻结原始合格参数行 {summary["source_rows"]}；折叠同一AR/时刻的等价记录后 '
             f'{summary["canonical_AR_days"]} 个样本；冲突隔离 {summary["conflicts"]} 组。', '',
             '| 状态 | AR时间样本数 |', '|---|---:|']
    lines += [f'| {label} | {summary["counts"].get(key, 0)} |' for key, label in names.items()]
    lines += ['', f'使用 {len(summary["coverage_sources"])} 个已锁定哈希的逐日GOES文件。'
              '这只是试点日期；未下载其他日期的覆盖资料不代表原始数据集没有这些样本。', '',
              f'在可确定的新目标中，{summary["target_changes_among_determinate"]} 个与原参考标签不同。'
              '两者等级校准和目录口径不同；不能据此直接认定原作者标错，也没有覆盖旧标签。', '',
              '## 判定依据', '',
              '- 目标为该NOAA活动区在 `(T,T+24h]` 起始、峰值达到科学校准M1.0以上的事件。',
              '- UTC分钟级事件起始时间若跨窗口边界，标为不确定；不是自动归到任一侧。',
              '- 四位NOAA编号只在前后14日有唯一已报告完整编号时关联。未归属事件不强行分配。',
              '- 负例要求所有分钟有至少一颗卫星的有效XRS-B平均：质量标志为0、数值有效、样本数非零。'
              '未插值、不填空，源文件的每分钟采样数另行保留。',
              '- 完整的一分钟产品覆盖不等于每秒都观测完整，不证明当时业务端已经获得这些事后重处理资料。',
              '- 未标注、没有目录记录、没有下载覆盖资料三者都不能单独证明是负例。', '',
              f'科学目录有 {summary["official_M_plus_events"]} 个M+事件，其中 '
              f'{summary["M_plus_without_reported_AR"]} 个没有报告AR；结合本地AR时段后仍有 '
              f'{summary["event_region_states"].get("unresolved", 0)} 个无法唯一关联，'
              '后者还包括不在本地AR样本时段内的已编号事件。', '',
              f'{summary["M_plus_end_not_reported"]} 个M+没有结束时间；本任务使用起始窗口和峰值等级，'
              '因此保留空值，不虚构结束时刻。', '', '## 已有完整覆盖的真实样本', '',
              '| 输入时刻 UTC | NOAA | 结果 |', '|---|---:|---|']
    lines += [f'| {r["time_utc"]} | {r["noaa"]} | {names[r["state"]]} |'
              for r in samples if r['coverage']['complete']][:30]
    known = [e for e in events if e['start'].startswith('2020-11-29') and
             e['class'] == 'M4.4' and not e['active_region_raw'].strip()]
    if known:
        ids = {e['id'] for e in known}
        affected = sum(r['coverage']['complete'] and any(e['id'] in ids for e in r['uncertain_events'])
                       for r in samples)
        lines += ['', '2020-11-29 M4.4事件在权威目录中存在，但没有报告AR。覆盖该事件的5个本地AR窗口'
                  .replace('5个', f'{affected}个') +
                  '有完整的质量分钟观测，但仍保持归属不确定；不能强行分配，也不能全部当负例。']
    lines += ['', '## 后续门槛', ''] + [f'- {gate}' for gate in summary['remaining_gates']]
    lines += ['', '旧标签、模型包与已完成实验不变。`samples.jsonl`可定位每个结论的时间窗、'
              '来源文件、事件编号和不确定原因。当前结果不能发布为空间天气业务预测。', '']
    return '\n'.join(lines)


def audit(dataset, catalogue, coverage_dir, output):
    dataset, catalogue, coverage_dir = map(Path, (dataset, catalogue, coverage_dir))
    output = output_path(output, dataset)
    for protected in (catalogue.resolve(), coverage_dir.resolve()):
        if output == protected or output.is_relative_to(protected) or protected.is_relative_to(output):
            raise ValueError('审计输出必须与来源缓存目录分开')
    if output.exists():
        raise ValueError('审计版本已经存在，禁止覆盖；请使用新目录')
    rows = [r for r in read_frozen(dataset, 'forecast-ready.jsonl') if r['eligible']]
    universe = read_frozen(dataset, 'forecast-all.jsonl')
    events, identities = official_events(catalogue)
    events = resolve_regions(events, universe)
    coverage = load(coverage_dir)
    supervisor = Supervisor(events, coverage)
    result = [supervisor.sample(row) for row in rows]
    # Do not silently inflate sample size when the source has duplicate rows.
    groups = defaultdict(list)
    for item in result:
        groups[item['sample_id']].append(item)
    canonical, conflicts = [], []
    for identity, members in groups.items():
        comparable = {json.dumps({k: x[k] for k in ('split', 'official_target', 'state', 'old_reference_target', 'source_feature_sha256')},
                                 sort_keys=True) for x in members}
        if len(comparable) != 1:
            conflicts.append({'sample_id': identity, 'rows': members})
            continue
        canonical.append({**members[0], 'source_record_count': len(members),
                          'source_parameters': [m['source_parameters'] for m in members]})
    summary = {'state': 'audit_completed_preparation_incomplete', 'created_utc': now(),
               'scope': 'metadata supervision audit; no training, inference or test metric evaluation',
               'target': 'reported NOAA AR, science M1.0+ peak class, onset in (T,T+24h]',
               'time_policy': 'UTC, reported start minute treated as [start,start+60s); boundary ambiguous',
               'class_policy': 'NCEI composite science-calibrated class, not historical operational class',
               'region_policy': 'unique reported full NOAA identity within +/-14 days, modulo match only',
               'negative_policy': 'catalogue-defined absence with complete good one-minute products; '
                                  'not proof of continuous second-level sampling or physically exhaustive events',
               'source_rows': len(rows), 'canonical_AR_days': len(canonical), 'conflicts': len(conflicts),
               'counts': dict(Counter(r['state'] for r in canonical)),
               'by_split': {s: dict(Counter(r['state'] for r in canonical if r['split'] == s))
                            for s in ('train', 'validation', 'calibration', 'test')},
               'target_changes_among_determinate': sum(r['target_changed'] for r in canonical),
               'official_M_plus_events': len(events),
               'M_plus_without_reported_AR': sum(not e['active_region_raw'].strip() for e in events),
               'M_plus_end_not_reported': sum(e['end'] is None for e in events),
               'duplicate_candidate_records': sum(e['catalogue_duplicate_candidate'] for e in events),
               'event_region_states': dict(Counter(e['region_basis'] for e in events)),
               'catalogue_snapshots': identities,
               'frozen_manifest_sha256': digest(dataset/'manifest-hashes.json'),
               'coverage_sources': [{k: v for k, v in source.items() if k != 'intervals'}
                                    for source in coverage.sources],
               'superseded_daily_sources': coverage.ignored_sources,
               'coverage_index_sha256': None, 'old_labels_changed': False,
               'training_started': False, 'test_scores_computed': False,
               'model_operational': False,
               'remaining_gates': ['补齐所有拟用窗口的质量分钟覆盖', '未归属M+及重复事件核验',
                                   '数值输入的历史可获得时间与实时提取适配',
                                   '已打开旧测试不能作为新方案独立测试；需新冻结外部时间段']}
    output.mkdir(parents=True)
    atomic_json(output/'coverage-index.json', coverage.sources)
    summary['coverage_index_sha256'] = digest(output/'coverage-index.json')
    for name, data in [('samples.jsonl', canonical), ('conflicts.jsonl', conflicts),
                       ('events.jsonl', events)]:
        raw = ''.join(json.dumps(r, ensure_ascii=False, allow_nan=False)+'\n' for r in data).encode('utf-8')
        commit(output/name, lambda stream, raw=raw: stream.write(raw))
    commit(output/'REPORT.md', lambda stream: stream.write(report_text(summary, canonical, events).encode('utf-8')))
    summary['artifacts'] = {p.name: digest(p) for p in output.iterdir() if p.is_file()}
    summary['code'] = {p.name: digest(p) for p in (Path(__file__), Path(__file__).with_name('xrs_coverage.py'))}
    atomic_json(output/'summary.json', summary)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', required=True)
    parser.add_argument('--catalogue', required=True)
    parser.add_argument('--coverage', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    from .runtime import configure_environment
    configure_environment()
    report = audit(args.dataset, args.catalogue, args.coverage, args.output)
    print(json.dumps({k: report[k] for k in ('state', 'canonical_AR_days', 'counts', 'event_region_states')},
                     ensure_ascii=False, indent=2))
