"""Audit whether known future flare sources were visible and detectable at issue time."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from pathlib import Path

import numpy as np
from PIL import Image
from safetensors.torch import load_file
from scipy.ndimage import label
import torch

from .ar_hmi_registration import DISPLAY_RADIUS, fits_field, source_grid
from .ar_nrt_feature_replay import prediction
from .ar_scale_study import ROOT, package_info
from .ar_srs_geometry_audit import load_coordinates, matched_ids, unique_links
from .datasets import SegmentationDataset
from .durable import atomic_json
from .forecast_ar_los_archive import local_file
from .forecast_region_join import load_region_mapping
from .forecast_srs_availability import ARCHIVE, fetch, lookup_day, parse_srs
from .journal import read
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path
from .xrs_coverage import utc


def srs_receipt(day, directories, archive):
    filename = day.strftime('%Y%m%dSRS.txt')
    expected_url = f'{ARCHIVE}/{day:%Y/%m}/{filename}'
    valid = {}
    for folder in directories:
        path = folder/filename
        if path.is_file():
            identity = read(path.with_suffix('.source.json'),{})
            sha = digest(path)
            if identity.get('url')==expected_url and identity.get('sha256')==sha:
                valid[sha] = path
    if len(valid)>1:
        return {'state':'conflicting_cached_revisions','digests':sorted(valid)}
    if valid:
        sha,path = next(iter(valid.items()))
        raw = path.read_bytes()
        report = parse_srs(raw,day)
    else:
        try:
            report,sha = fetch(day,archive)
            raw = (archive/filename).read_bytes()
        except (OSError,ValueError) as error:
            return {'state':'archive_unavailable','error_type':type(error).__name__}
    return {'state':'ready','report':report,'raw':raw,'sha256':sha,'url':expected_url}


def find_reference(number, stem, reference, mapping):
    labels = [int(value) for value in np.unique(reference) if value>0 and number in mapping.get((stem,int(value)),())]
    if len(labels)!=1 or mapping[(stem,labels[0])]!={number}:
        return None
    return labels[0]


def projection(receipt, when, header, registration, components, assigned):
    if receipt['state']!='ready':
        return {'state':receipt['state']}
    report = receipt['report']
    issued = utc(report['issued_utc'])
    if issued>when or report['corrected_marker']:
        return {'state':'unavailable_original_release','published':report['issued_utc'],'sha256':receipt['sha256']}
    positions = load_coordinates(receipt['raw'],report,when,header,registration)
    links, ambiguous = unique_links(positions,components)
    return {'state':'ready','published':report['issued_utc'],'sha256':receipt['sha256'],'url':receipt['url'],
            'positions':positions,'links':links,'ambiguous_candidates':ambiguous,'reference_matches':assigned}


def event_result(event, stem, reference, core, mapping, assigned, projections):
    result = {'event_id':event['id'],'start':event['start'],'class':event['class'],
              'noaa':event['resolved_noaa'],'event_attribution_basis':event['region_basis']}
    if event['resolved_noaa'] is None:
        return {**result,'reference_state':'event_noaa_unresolved','srs':{}}
    number = event['resolved_noaa']
    instance = find_reference(number,stem,reference,mapping)
    component = next((k for k,v in assigned.items() if v==instance),None)
    fraction = float(core[reference==instance].mean()) if instance is not None else None
    srs = {}
    for phase,row in projections.items():
        if row['state']!='ready':
            srs[phase] = row['state']
            continue
        short = number%10000
        if short not in row['positions']:
            srs[phase] = 'not_projected'
        elif component is None:
            srs[phase] = 'no_verified_detection'
        elif row['links'].get(component)==short:
            srs[phase] = 'agrees_with_event_and_reference'
        else:
            srs[phase] = 'no_unique_agreeing_link'
    return {**result,'reference_state':('strict_match' if component is not None else
        ('reference_region_not_mapped' if instance is None else 'no_strict_match')),
        'reference_id':instance,'matched_component':component,'reference_inside_prior_core_fraction':fraction,'srs':srs}


def run(dataset, package, forecast, audit, output):
    output = output_path(output,dataset)
    initial = forecast/'ar-los-issue-physical-01'
    received = read(initial/'input-receipt.json')
    source = read(initial/'summary.json')
    physical = read(initial/'protocol.json')
    events_file = audit/'events.jsonl'
    delay = forecast/'full-disk-availability-delay-01'/'windows.json'
    archive_hash = read(audit/'summary.json')['artifacts']['events.jsonl']
    if ((output/'summary.json').exists() or digest(events_file)!=archive_hash or digest(initial/'input-receipt.json')!=physical['receipt_sha256']
            or digest(initial/'protocol.json')!=source['protocol_sha256'] or source['test_accessed']
            or received['dataset_sha256']!=digest(dataset/'manifest-hashes.json')
            or received['inputs'][delay.resolve().relative_to(ROOT).as_posix()]!=digest(delay)):
        raise ValueError('事件、预报回放或冻结分割数据身份不符')
    windows = {r['uri']:r for r in read(delay)['70']}
    events = {}
    with events_file.open(encoding='utf-8') as stream:
        for line in stream:
            item = json.loads(line)
            events[item['id']] = item
    development = {s:SegmentationDataset(dataset,'AR',s,512) for s in ('train','validation')}
    rows_by_uri = {r['uri']:r for data in development.values() for r in data.rows}
    mapping_path = development['train'].source('@ssafd/AR/MASK-NOAA.xlsx')
    mapping = load_region_mapping(mapping_path)
    numbered = [r for r in received['samples'] if r['target']==1]
    times = sorted({lookup_day(utc(r[key])) for r in numbered for key in ('observed_utc','issue_utc')})
    card, constructor, weights = package_info(package)
    if digest(weights)!=physical['weights_sha256']:
        raise ValueError('需要已用于物理量对照的冻结活动区定位器')
    settings = {'code_sha256':digest(__file__),'forecast_input_sha256':digest(initial/'input-receipt.json'),
        'physical_summary_sha256':digest(initial/'summary.json'),'event_sha256':archive_hash,
        'reference_noaa_sha256':digest(mapping_path),'dates':[str(t) for t in times],
        'windows':[r['uri'] for r in numbered],
        'rule':'event NOAA and reference only for scoring; match detector IoU >= .5; prior SRS projected using original HMI receipt',
        'test_accessed':False,'calibration_accessed':False,'issued_forecast':False}
    previous = read(output/'protocol.json')
    if previous is None:
        atomic_json(output/'protocol.json',settings)
    elif previous!=settings:
        raise ValueError('历史归属审计协议已改变')
    caches = [p for p in forecast.glob('*/srs') if p.is_dir() and p!=output/'srs']
    with ThreadPoolExecutor(max_workers=6) as pool:
        reports = dict(zip(times,pool.map(lambda d:srs_receipt(d,caches,output/'srs'),times),strict=True))
    atomic_json(output/'srs-status.json',{str(day):{k:v for k,v in row.items() if k not in ('raw','report')}
                                         for day,row in reports.items()})
    model,_ = constructor(card['config'])
    model.load_state_dict(load_file(weights))
    torch.set_num_threads(2)
    results = []
    grids = np.ogrid[:1024,:1024]
    core = sum((coord-511.5)**2 for coord in grids) <= (.72*DISPLAY_RADIUS)**2
    with exclusive(ROOT/'.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.backends.cuda.matmul.allow_tf32=False
        torch.backends.cudnn.allow_tf32=False
        model.cuda().eval()
        meter = Resources()
        for row in numbered:
            uri = row['uri']
            split = row['split']
            entry = rows_by_uri[uri]
            if entry['split']!=split or utc(entry['time_utc'])!=utc(row['observed_utc']):
                raise ValueError('活动区成像时间或划分改变')
            start,end = utc(row['issue_utc']),utc(row['target_end_utc'])
            window_events = [events[k] for k in windows[uri]['positive_event_ids']]
            if not window_events or not all(start<utc(e['start']) and utc(e['start'])+timedelta(seconds=e['time_resolution_seconds'])<=end for e in window_events):
                raise ValueError('事件不在冻结目标窗')
            archive = local_file(row['fits'],row['fits_sha256'])
            review = read(archive.parent/'review.json')
            if review['uri']!=uri or review['array_sha256']!=row['array_sha256'] or not review['registration']['accepted']:
                raise ValueError('磁图配准记录不符')
            _, header = fits_field(archive)
            source_grid(header)
            file = development[split].source(uri)
            if digest(file)!=row['source_jpeg_sha256']:
                raise ValueError('图像字节改变')
            with Image.open(file) as image:
                gray = np.asarray(image.convert('L'))
            mask = prediction(model,gray)
            reference_file = development[split].source(entry['mask_uri'])
            reference = np.load(reference_file,allow_pickle=False)
            key,value = np.unique(reference,return_counts=True)
            counts = {str(int(k)):int(v) for k,v in zip(key,value,strict=True) if k>0}
            expected = {k:v for k,v in entry['mask_statistics']['pixels'].items() if int(k)>0}
            if reference.shape!=gray.shape or counts!=expected:
                raise ValueError('参考掩膜统计或坐标改变')
            components,_ = label(mask)
            assigned = matched_ids(components,reference)
            schedule = {'observation':utc(row['observed_utc']),'issue':start}
            projections = {phase:projection(reports[lookup_day(t)],t,header,review['registration'],components,assigned)
                           for phase,t in schedule.items()}
            comparisons = [event_result(e,Path(uri).stem,reference,core,mapping,assigned,projections) for e in window_events]
            results.append({'uri':uri,'split':split,'observed_utc':row['observed_utc'],'issue_utc':row['issue_utc'],
                'reference_sha256':digest(reference_file),'srs':projections,'events':comparisons})
            meter.sample()
        resources = meter.sample()
    by_split = {}
    for split in development:
        cases = [r for r in results if r['split']==split]
        entries = [e for r in cases for e in r['events']]
        counters = Counter(e['reference_state'] for e in entries)
        by_split[split] = {'windows':len(cases),'event_occurrences':len(entries),'events_unique':len({e['event_id'] for e in entries}),
            'reference':dict(counters),'windows_with_any_strict_detection':sum(any(e['reference_state']=='strict_match' for e in r['events']) for r in cases),
            'strict_matches_mostly_inside_prior_core':sum(e['reference_state']=='strict_match' and e['reference_inside_prior_core_fraction']>=.5 for e in entries),
            'srs':{p:dict(Counter(e['srs'][p] for e in entries if p in e['srs'])) for p in ('observation','issue')}}
    atomic_json(output/'cases.json',results)
    atomic_json(output/'summary.json',{'state':'completed','recorded_utc':now(),'by_split':by_split,
        'srs_files':dict(Counter(r['state'] for r in reports.values())),'protocol_sha256':digest(output/'protocol.json'),
        'cases_sha256':digest(output/'cases.json'),'resources':resources,'test_accessed':False,
        'calibration_accessed':False,'issued_forecast':False})


if __name__=='__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset','package','forecast','audit','output'):
        parser.add_argument('--'+name,required=True,type=Path)
    run(**vars(parser.parse_args()))
