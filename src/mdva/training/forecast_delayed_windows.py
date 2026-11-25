"""Rebuild regional GOES labels after a fixed input-availability delay."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import timedelta
import json
from pathlib import Path

from .durable import atomic_json
from .forecast_supervision_audit import Supervisor
from .journal import read
from .runtime import digest, now, output_path
from .xrs_coverage import Coverage, utc


ISSUE_DELAY = timedelta(hours=2)
HORIZON = timedelta(hours=24)


def delay_window(row):
    observed = utc(row['observed_utc'])
    if utc(row['target_end_utc']) != observed + HORIZON:
        raise ValueError('原目标期限非24小时')
    issue = observed + ISSUE_DELAY
    return issue, issue + HORIZON


def relabel(row, supervisor, delayed=True):
    if row['split'] not in ('train','validation') or row['target'] not in (0,1):
        raise ValueError('时效实验仅接受冻结训练或开发已核验事件')
    start,end = delay_window(row)
    if not delayed:
        start,end = utc(row['observed_utc']),utc(row['target_end_utc'])
    origins = {'split':row['split'],'noaa':row['noaa'],'time_utc':start.isoformat(),
               'target_start':start.isoformat(),'target_end':end.isoformat(),'target_m_plus':row['target'],
               'parameters_uri':row['uri'],'parameter_line':None}
    outcome = supervisor.sample(origins)
    return {'uri':row['uri'],'split':row['split'],'noaa':row['noaa'],
            'observed_utc':row['observed_utc'],'issue_utc':start.isoformat(),'target_end_utc':end.isoformat(),
            'old_target':row['target'],'target':outcome['official_target'],'state':outcome['state'],
            'positive_event_ids':outcome['positive_events'],'uncertain_events':outcome['uncertain_events'],
            'coverage':outcome['coverage'],'features':row['features'],
            'input_availability_at_issue_verified':False}


def inputs(folder):
    records = []
    references = []
    groups = (('ar-srs-candidate-forecast-01','matched-outcomes.json','outcomes_sha256'),
              ('ar-srs-fresh-01','new-outcomes.json','new_outcomes_sha256'),
              ('ar-srs-remaining-01','new-outcomes.json','new_outcomes_sha256'))
    for leaf,name,key in groups:
        src = folder/leaf
        summary = read(src/'summary.json')
        rows = src/name
        if (summary['state']!='completed' or summary['test_accessed'] or summary['calibration_accessed']
                or summary[key]!=digest(rows)):
            raise ValueError('已有活动区预测研究来源身份不符')
        records.extend(read(rows))
        references.append({'path':str(rows),'sha256':summary[key]})
    ids = {(r['split'],r['uri'],r['noaa']) for r in records}
    if len(ids)!=len(records) or any(r['split'] not in ('train','validation') for r in records):
        raise ValueError('活动区预测样本存在重复或未经放行的划分')
    return records,references


def run(dataset, folder, audit, output):
    output = output_path(output,dataset)
    if output.exists():
        raise ValueError('使用独立的延迟窗审计目录')
    rows,sources = inputs(folder)
    receipt = read(audit/'summary.json')
    coverage_path = audit/'coverage-index.json'
    if (receipt['frozen_manifest_sha256']!=digest(dataset/'manifest-hashes.json')
            or receipt['coverage_index_sha256']!=digest(coverage_path)):
        raise ValueError('预测事件覆盖源不对应同一冻结清单')
    earliest = min(utc(row['observed_utc']) for row in rows)-timedelta(minutes=1)
    latest = max(delay_window(row)[1] for row in rows)
    observed_sources = [entry for entry in read(coverage_path)
                        if utc(entry['time_coverage_start'])<=latest and utc(entry['time_coverage_end'])>=earliest]
    coverage = Coverage(observed_sources)
    catalog = {entry['name']:entry['sha256'] for entry in receipt['catalogue_snapshots']}
    events = []
    with (audit/'events.jsonl').open(encoding='utf-8') as stream:
        for event in map(json.loads,stream):
            if earliest<=utc(event['start'])<=latest:
                if catalog.get(event['source_file'])!=event['source_sha256']:
                    raise ValueError('事件目录引用不存在或指纹不符')
                events.append(event)
    supervisor = Supervisor(events,coverage)
    actual = [relabel(row,supervisor) for row in rows]
    control = [relabel(row,supervisor,False) for row in rows]
    if any(row['target']!=base['target'] for row,base in zip(rows,control,strict=True)):
        raise ValueError('已知零延迟窗口未能重现，先检查监督源差异')
    first_dev = min(utc(row['issue_utc']) for row in actual if row['split']=='validation')
    dev_numbers = {row['noaa'] for row in actual if row['split']=='validation'}
    for row in actual:
        row['excluded_by_split_purge'] = (row['split']=='train'
                and (utc(row['target_end_utc'])>=first_dev or row['noaa'] in dev_numbers))
    statistics = {}
    for lane in ('train','validation'):
        group = [r for r in actual if r['split']==lane]
        eligible = [r for r in group if r['target'] in (0,1) and not r['excluded_by_split_purge']]
        statistics[lane] = {'windows':len(group),'base_positives':sum(r['old_target']==1 for r in group),
                            'known_after_delay':len(eligible),'positives_after_delay':sum(r['target']==1 for r in eligible),
                            'changed_between_known':sum(r['target'] in (0,1) and r['target']!=r['old_target'] for r in group),
                            'target_states':dict(Counter(r['state'] for r in group)),
                            'purged':sum(r['excluded_by_split_purge'] for r in group)}
    protocol = {'inputs':sources,'event_audit_sha256':digest(audit/'summary.json'),
                'event_rows_sha256':digest(audit/'events.jsonl'),'coverage_sha256':digest(coverage_path),
                'issue_offset_minutes':120,'target':'M1.0+ GOES onset during (issue,issue+24h] by numbered region',
                'source_fields_carried_for_development_only':True,'test_accessed':False,'calibration_accessed':False}
    atomic_json(output/'protocol.json',protocol)
    atomic_json(output/'windows.json',actual)
    report = {'state':'completed','created_utc':now(),'results':statistics,'zero_delay_reproduced':len(control),
              'windows_sha256':digest(output/'windows.json'),'protocol_sha256':digest(output/'protocol.json'),
              'verified_nrt_asof_inputs':False,'model_trained':False,'predictor_released':False,
              'test_accessed':False,'calibration_accessed':False}
    atomic_json(output/'summary.json',report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('dataset','folder','audit','output'):
        parser.add_argument('--'+key,required=True,type=Path)
    print(json.dumps(run(**vars(parser.parse_args())),ensure_ascii=False))


if __name__ == '__main__':
    main()
