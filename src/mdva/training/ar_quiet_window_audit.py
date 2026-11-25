"""Measure active-region candidate alerts on independently screened quiet-Sun dates."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from pathlib import Path
import re

import numpy as np
from PIL import Image
from scipy.ndimage import find_objects, label

from .ar_hmi_registration import DISPLAY_RADIUS, review
from .datasets import read_frozen, resolve_uri
from .durable import atomic_json
from .forecast_ar_event_attribution import srs_receipt
from .forecast_srs_availability import lookup_day, utc
from .inference import load_model, predict_loaded
from .journal import read
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


ROOT = Path(__file__).resolve().parents[3]
YEARS = ('2018', '2019', '2020')
REGION_MARKER = re.compile(r'^I(?:A)?\.\s', re.M)
FIELD_FLOOR = 100.
MIN_STRONG_PIXELS = 16


def select(rows, source_hashes):
    represented = {r.get('pixel_sha256') for r in rows if r.get('mask_uri')}
    represented_times = {r.get('time_utc') for r in rows if r['category']=='AR' and r.get('mask_uri')}
    groups = defaultdict(list)
    for row in rows:
        if (row['category']=='AR' and row['split']=='train' and row['date'][:4] in YEARS
                and row['exclusions']==['missing_or_invalid_mask'] and not row.get('mask_uri')
                and not row['noaa_ids'] and row.get('pixel_sha256') not in represented
                and row['time_utc'] not in represented_times and row['uri'] in source_hashes
                and row['time_basis']=='tai_explicit' and (row['width'],row['height'])==(1024,1024)):
            groups[row['date'][:7]].append(row)
    selected = []
    for _, items in sorted(groups.items()):
        chosen = []
        for target in (8,22):
            options = [r for r in items if abs(int(r['date'][8:])-target)<=5]
            match = min(options,key=lambda r:(abs(int(r['date'][8:])-target),r['date']),default=None)
            if match is not None:
                chosen.append(match)
        selected.extend(chosen)
    return [{'uri':r['uri'],'time_utc':r['time_utc'],'sha256':source_hashes[r['uri']],
             'id':r['date']} for r in selected]


def explicit_empty(receipt):
    if receipt['state']!='ready' or receipt['report']['visible_regions'] or receipt['report']['corrected_marker']:
        return False
    body = receipt['raw'].decode('ascii').split('\nII.',1)
    if len(body)!=2:
        return False
    sections = REGION_MARKER.split(body[0])
    return len(sections)==3 and all(
        sum(line.strip()=='None' for line in section.splitlines())==1
        and not re.search(r'no (?:solar )?observations|no data|unknown',section,re.I)
        for section in sections[1:])


def classify_pair(instant, before, after):
    if any(r['state']!='ready' for r in (before,after)):
        return 'reports_unavailable'
    left,right = [utc(r['report']['issued_utc']) for r in (before,after)]
    clock_limit = timedelta(hours=26)
    if not (left<=instant<right and instant-left<=clock_limit and right-instant<=clock_limit):
        return 'report_timing_mismatch'
    return 'srs_quiet_window' if explicit_empty(before) and explicit_empty(after) else 'not_explicitly_quiet'


class LocalImages:
    def __init__(self,sources):
        self.sources = sources

    def source(self,uri):
        return resolve_uri(uri,self.sources)


def physical_stats(binary, field):
    components,count = label(binary)
    y,x = np.ogrid[:binary.shape[0],:binary.shape[1]]
    disk = (x-511.5)**2+(y-511.5)**2<=DISPLAY_RADIUS**2
    finite = disk & np.isfinite(field)
    regions = []
    for number,region_slice in enumerate(find_objects(components),1):
        area = components[region_slice]==number
        valid = area & finite[region_slice]
        values = field[region_slice][valid]
        plus,minus = [int((part>=FIELD_FLOOR).sum()) for part in (values,-values)]
        regions.append({'component':number,'pixels':int(area.sum()),'field_pixels':len(values),
            'strong_positive_pixels':plus,'strong_negative_pixels':minus,
            'both_signs_supported':min(plus,minus)>=MIN_STRONG_PIXELS,
            'abs_field_p95_gauss':float(np.percentile(np.abs(values),95)) if len(values) else None,
            'bbox_xyxy':[region_slice[1].start,region_slice[0].start,region_slice[1].stop,region_slice[0].stop]})
    if len(regions)!=count:
        raise ValueError('候选连通区域计数不一致')
    return regions


def run(dataset,package,forecast,output):
    configure_environment()
    output = output_path(output,dataset)
    images = LocalImages(read(dataset/'sources.json'))
    files = read_frozen(dataset,'source-files.jsonl')
    selected = select(read_frozen(dataset,'segmentation-all.jsonl'),{r['uri']:r['sha256'] for r in files})
    if not selected or (output/'summary.json').exists():
        raise ValueError('需要新研究版本与冻结图像清单')
    policy = {'state':'frozen','dataset_sha256':digest(dataset/'manifest-hashes.json'),
        'code_sha256':digest(__file__),'package_sha256':digest(package/'model.json'),
        'selection':selected,'date_sampling':'nearest to day 8 and 22 within 5 days; 2018–2020; no reviewed reference or duplicate',
        'reference_scope':'two adjacent explicit-empty NOAA/USAF SRS I and IA reports, plus quality-controlled HMI context; no pixel truth',
        'field_proxy_only':{'abs_gauss':FIELD_FLOOR,'minimum_pixels_each_sign':MIN_STRONG_PIXELS},
        'probability_threshold':.5,'test_accessed':False,'calibration_accessed':False}
    if (output/'protocol.json').exists() and read(output/'protocol.json')!=policy:
        raise ValueError('审计输入或代码已变化，请新建输出版本')
    atomic_json(output/'protocol.json',policy)
    sources = [folder for folder in forecast.glob('*/srs') if folder.is_dir()]
    dates = {r['id']:utc(r['time_utc']) for r in selected}
    days = {lookup_day(t)+timedelta(days=d) for t in dates.values() for d in (0,1)}
    days = sorted(days)
    with ThreadPoolExecutor(max_workers=6) as pool:
        bulletins = dict(zip(days,pool.map(lambda day:srs_receipt(day,sources,output/'srs'),days),strict=True))
    records = []
    ready = []
    for row in selected:
        stamp = dates[row['id']]
        prior = lookup_day(stamp)
        before,after = [bulletins[prior+timedelta(days=d)] for d in (0,1)]
        case = {**row,'reference_state':classify_pair(stamp,before,after),
                'reports':[{k:v for k,v in record.items() if k!='raw'} for record in (before,after)]}
        if case['reference_state']=='srs_quiet_window':
            ready.append(case)
        records.append(case)
    atomic_json(output/'screening.json',records)

    candidates = []
    for row in ready:
        folder = output/'hmi'/row['id']
        image = images.source(row['uri'])
        if digest(image)!=row['sha256']:
            raise ValueError('显示磁图与冻结清单不符')
        review_path = folder/'review.json'
        record = read(review_path)
        if record is None:
            record = review(images,row,'quiet_screening',folder,None)
            atomic_json(review_path,record)
        row['physical'] = record
        if record['state']=='display_alignment_supported':
            candidates.append((row,image,folder/'aligned-los.npy'))
        atomic_json(output/'screening.json',records)
    inputs = {'protocol_sha256':digest(output/'protocol.json'),'screening_sha256':digest(output/'screening.json'),
              'cases':[r['id'] for r,_,_ in candidates]}
    atomic_json(output/'cohort.json',inputs)
    statuses = Counter(r['reference_state'] for r in records)
    physical = Counter(r['physical']['state'] for r in ready)
    rows = []
    if candidates:
        import torch

        with exclusive(ROOT/'.mdva/training-gpu.lock'), torch.inference_mode():
            model,card = load_model(package)
            if card['id']!='AR-A0-s42':
                raise ValueError('只审计当前平台活动区候选模型')
            meter = Resources()
            for row,image,physical_array in candidates:
                if digest(physical_array)!=row['physical']['array_sha256']:
                    raise ValueError('同刻物理磁图缓存改变')
                destination = output/'predictions'/row['id']
                result = read(destination/'candidates.json')
                if result is None:
                    result = predict_loaded(model,card,image,'magnetogram',destination)
                mask_path = destination/'candidate-mask.png'
                if (result['image_sha256']!=row['sha256']
                        or digest(mask_path)!=result['assets']['candidate-mask.png']):
                    raise ValueError('独立候选缓存身份不符')
                field = np.load(physical_array,allow_pickle=False)
                with Image.open(mask_path) as mask:
                    regions = physical_stats(np.asarray(mask)>0,field)
                rows.append({'id':row['id'],'asset_sha256':digest(mask_path),'regions':regions,
                             'self_check':result['self_check']})
                atomic_json(output/'cases.json',rows)
            resources = meter.sample()
    else:
        resources = None
    atomic_json(output/'summary.json',{'state':'completed','recorded_utc':now(),
        'selected':len(selected),'reference_status':dict(statuses),'physical_status':dict(physical),
        'reviewed_images':len(rows),'images_with_candidates':sum(bool(r['regions']) for r in rows),
        'images_with_bipolar_proxy':sum(any(c['both_signs_supported'] for c in r['regions']) for r in rows),
        'total_candidates':sum(len(r['regions']) for r in rows),
        'candidates_with_bipolar_proxy':sum(c['both_signs_supported'] for r in rows for c in r['regions']),
        'cohort_sha256':digest(output/'cohort.json'),'cases_sha256':digest(output/'cases.json') if rows else None,
        'resources':resources,'test_accessed':False,'calibration_accessed':False,
        'scope':'Independent report quiet-window candidate frequency; not a verified physical false-positive rate.'})


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset','package','forecast','output'):
        parser.add_argument('--'+name,required=True,type=Path)
    run(**vars(parser.parse_args()))
