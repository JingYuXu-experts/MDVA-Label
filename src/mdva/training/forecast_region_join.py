"""Audit retrospective links from AR reference regions to forecast supervision."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import json
from pathlib import Path

import openpyxl

from .datasets import SegmentationDataset, read_frozen
from .durable import atomic_json, commit
from .provenance import integer
from .runtime import digest, now, output_path

DEVELOPMENT_SPLITS = ('train', 'validation')


def load_region_mapping(path):
    mapping = defaultdict(set)
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        rows = book.active.iter_rows(values_only=True)
        columns = [str(key).strip().upper() for key in next(rows)]
        if not {'FILENAME', 'MASK_NUM', 'NOAA_AR'} <= set(columns):
            raise ValueError('原始活动区对应表缺少必需列')
        for values in rows:
            row = dict(zip(columns, values))
            if not isinstance(row['FILENAME'], str):
                continue
            region, noaa = integer(row['MASK_NUM']), integer(row['NOAA_AR'])
            if region is not None and region > 0 and noaa is not None and noaa >= 10000:
                stem = Path(row['FILENAME'].replace('\\', '/')).stem
                mapping[(stem, region)].add(noaa)
    finally:
        book.close()
    return mapping


def association(parameter, images, mapping):
    key = (parameter['stem'], parameter['time_utc'])
    candidates = images.get(key, [])
    if len(candidates) != 1:
        return ('no_exact_development_image' if not candidates else 'ambiguous_image'), None
    image = candidates[0]
    instance = parameter['instance_id']
    if image['instrument'] != 'HMI' or parameter['instrument'] != 'HMI':
        return 'unsupported_instrument', None
    if not image['mask_statistics']['pixels'].get(str(instance), 0):
        return 'reference_instance_missing', None
    mapped = mapping.get((parameter['stem'], instance), set())
    if mapped != {parameter['noaa']}:
        return 'reference_noaa_not_unique_or_conflicting', None
    return 'exact_reference_join', image


def run(dataset, audit, output):
    dataset, audit = Path(dataset).resolve(), Path(audit).resolve()
    output = output_path(output, dataset)
    if output.exists() or output.is_relative_to(audit):
        raise ValueError('请用独立的新目录保存对应审计')
    samples_file = audit/'samples.jsonl'
    samples_sha = digest(samples_file)
    data = [SegmentationDataset(dataset, 'AR', split) for split in DEVELOPMENT_SPLITS]
    mapping_file = data[0].source('@ssafd/AR/MASK-NOAA.xlsx')
    mapping = load_region_mapping(mapping_file)
    images = defaultdict(list)
    for split in data:
        for image in split.rows:
            images[(Path(image['uri']).stem, image['time_utc'])].append(image)

    parameters = defaultdict(list)
    for row in read_frozen(dataset, 'forecast-ready.jsonl'):
        if row['eligible'] and row['split'] in DEVELOPMENT_SPLITS:
            parameters[(row['parameters_uri'], row['parameter_line'])].append(row)

    rows = []
    seen = set()
    with samples_file.open(encoding='utf-8') as stream:
        for line in stream:
            sample = json.loads(line)
            if sample['split'] not in DEVELOPMENT_SPLITS:
                continue
            if sample['sample_id'] in seen:
                raise ValueError('事件审核存在重复样本编号')
            seen.add(sample['sample_id'])
            event_start = datetime.fromisoformat(sample['target_start'])
            event_end = datetime.fromisoformat(sample['target_end'])
            if sample['target_start'] != sample['time_utc'] or event_end-event_start != timedelta(hours=24):
                raise ValueError('事件目标窗改变')
            options = []
            for source in sample['source_parameters']:
                options.extend(parameters[(source['uri'], source['line'])])
            if not options or any((r['split'], r['time_utc'], r['noaa']) != (sample['split'], sample['time_utc'], sample['noaa']) for r in options):
                raise ValueError('事件审核与参数来源不一致')
            identities = {(p['stem'],p['instance_id']) for p in options}
            state, image = association(options[0], images, mapping) if len(identities)==1 else ('conflicting_reference_instances', None)
            rows.append({'sample_id':sample['sample_id'], 'split':sample['split'], 'time_utc':sample['time_utc'],
                         'noaa':sample['noaa'], 'join_state':state, 'target':sample['official_target'], 'target_state':sample['state'],
                         'image_uri':image['uri'] if image else None, 'mask_uri':image['mask_uri'] if image else None,
                         'reference_instance':options[0]['instance_id'] if image else None, 'detector_split':image['split'] if image else None,
                         'model_region_has_verified_noaa':False, 'operational_input_verified':False})
    if digest(samples_file)!=samples_sha:
        raise ValueError('读取期间事件审核文件改变')
    joined = [r for r in rows if r['join_state']=='exact_reference_join']
    output.mkdir(parents=True)
    report = {'created_utc':now(), 'state':'completed', 'scope':'Metadata-only train/development link audit. No predictions or application packages created.',
              'code_sha256':digest(__file__), 'frozen_manifest_sha256':digest(dataset/'manifest-hashes.json'),
              'reference_mapping_sha256':digest(mapping_file), 'supervision_sha256':samples_sha,
              'samples':len(rows), 'join_states':dict(Counter(r['join_state'] for r in rows)),
              'joined_by_forecast_split':{s:dict(Counter(str(r['target']) for r in joined if r['split']==s)) for s in DEVELOPMENT_SPLITS},
              'joined_by_detector_split':dict(Counter(r['detector_split'] for r in joined)),
              'matched_images':len({r['image_uri'] for r in joined}),
              'matched_noaa_regions':len({r['noaa'] for r in joined}),
              'limitations':['These associations use original reference IDs. A newly detected mask does not inherit a NOAA ID.',
                 'Future experiment needs out-of-fold detector outputs, independently verified observation availability, and a paired baseline-versus-region-feature comparison.',
                 'Actual time and source identity must match. No nearest-date, midnight approximation or unknown target treated as negative.'],
              'test_pixels_opened':False, 'test_targets_used':False, 'model_trained':False, 'operational_forecast_available':False}
    content = ''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in rows).encode('utf-8')
    commit(output/'development-joins.jsonl',lambda stream:stream.write(content))
    report['join_rows_sha256']=digest(output/'development-joins.jsonl')
    atomic_json(output/'summary.json',report)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--dataset',required=True)
    parser.add_argument('--audit',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    result=run(args.dataset,args.audit,args.output)
    print(json.dumps({k:result[k] for k in ('samples','join_states','joined_by_forecast_split','matched_images')},ensure_ascii=False))
