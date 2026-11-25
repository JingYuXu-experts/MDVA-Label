"""Audit and compare same-region delayed development windows with archived SHARP NRT data."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re

import numpy as np

from .ar_hmi_registration import FILENAME
from .durable import atomic_json
from .forecast_disk_xrs import scores, train_fit
from .forecast_srs_candidate import APERTURE, CANDIDATE, XRS
from .hmi_source_audit import tai_utc
from .journal import read
from .metrics import forecast_scores
from .runtime import digest, now, output_path
from .xrs_coverage import utc


NRT = ('nrt_log_usflux','nrt_log1p_area_acr','nrt_meangbt','nrt_r_value')
SOURCE_FIELDS = ('USFLUX','AREA_ACR','MEANGBT','R_VALUE')
QUALITY = {'0x00000000','0x00000400'}


def index_catalog(catalog,stamp):
    if catalog['status']!=0:
        raise ValueError('SHARP 目录状态无效')
    by_noaa=defaultdict(list)
    count=catalog['count']
    if not count:
        return by_noaa
    fields={col['name']:col['values'] for col in catalog['keywords']}
    if any(len(values)!=count for values in fields.values()) or set(fields['T_REC'])!={stamp}:
        raise ValueError('SHARP 目录长度或记录时刻发生变化')
    if len(set(fields['HARPNUM']))!=count:
        raise ValueError('同一时刻出现重复 HARP 编号')
    for i,value in enumerate(fields['NOAA_ARS']):
        if not re.fullmatch(r'\d+(?:\s*,\s*\d+)*',value):
            continue
        numbers={int(token) for token in value.split(',')}
        for noaa in numbers:
            by_noaa[noaa].append(({key:values[i] for key,values in fields.items()},len(numbers)))
    return by_noaa


def attach(row,current,stamp):
    if row['target'] not in (0,1) or row['excluded_by_split_purge']:
        return {'state':'target_unknown_or_purged'}
    when=tai_utc(stamp)
    if abs((utc(row['observed_utc'])-when).total_seconds())>1:
        return {'state':'observation_mismatch'}
    if not current:
        return {'state':'no_region_match'}
    if len(current)!=1 or current[0][1]!=1:
        return {'state':'ambiguous_or_multi_region_patch'}
    record=current[0][0]
    if record['QUALITY'] not in QUALITY:
        return {'state':'quality_excluded','quality':record['QUALITY']}
    try:
        generated=utc(record['DATE'])
        raw=[float(record[name]) for name in SOURCE_FIELDS]
    except (ValueError,KeyError,TypeError):
        return {'state':'missing_time_or_features'}
    if not when<=generated<=utc(row['issue_utc']):
        return {'state':'generated_after_cutoff_or_invalid'}
    if not np.isfinite(raw).all() or raw[0]<=0 or min(raw[1:])<0:
        return {'state':'invalid_features'}
    flux,area,gradient,r=raw
    features=dict(zip(NRT,(float(np.log10(flux)),float(np.log1p(area)),gradient,r),strict=True))
    return {'state':'matched_generated_before_cutoff','quality':record['QUALITY'],'harp':record['HARPNUM'],
            'generated_utc':generated.isoformat(),'catalog_creation_lag_seconds':(generated-when).total_seconds(),
            'features':{**row['features'],**features},'actual_historical_receipt_verified':False}


def evaluate(rows):
    by_time={split:[row for row in rows if row['split']==split and row['state']=='matched_generated_before_cutoff']
             for split in ('train','validation')}
    fit,held=by_time['train'],by_time['validation']
    counts={split:{'windows':len(group),'positives':sum(r['target'] for r in group),
                   'positive_active_regions':len({r['noaa'] for r in group if r['target']})}
            for split,group in by_time.items()}
    if (counts['train']['positives']<15 or len(fit)-counts['train']['positives']<15
            or counts['validation']['positive_active_regions']<3 or counts['validation']['positives']==len(held)):
        return {'state':'insufficient_known_outcomes','counts':counts}
    if (max(utc(r['target_end_utc']) for r in fit)>=min(utc(r['issue_utc']) for r in held)
            or {r['noaa'] for r in fit}&{r['noaa'] for r in held}):
        raise ValueError('模型训练与验证目标时段或活动区重叠')
    climate=np.mean([r['target'] for r in fit])
    y=np.array([r['target'] for r in held])
    arms={'xrs_and_aperture':(*XRS,*APERTURE),'xrs_aperture_and_mask':(*XRS,*APERTURE,*CANDIDATE),
          'xrs_aperture_and_nrt':(*XRS,*APERTURE,*NRT),'xrs_aperture_mask_and_nrt':(*XRS,*APERTURE,*CANDIDATE,*NRT)}
    metrics={key:forecast_scores(y,scores(train_fit(fit,features),held,features),climate=climate)
             for key,features in arms.items()}
    return {'state':'evaluated','counts':counts,'metrics':metrics}


def run(dataset,windows,intake,output):
    output=output_path(output,dataset)
    if (output/'summary.json').exists() or output.is_relative_to(windows):
        raise ValueError('以独立目录保存预测时效审计')
    prior=read(windows/'summary.json')
    catalog=read(intake/'intake.json')
    if (any(r['state']!='completed' or r['test_accessed'] or r['calibration_accessed'] for r in (prior,catalog))
            or prior['windows_sha256']!=digest(windows/'windows.json')
            or prior['protocol_sha256']!=digest(windows/'protocol.json')
            or catalog['delay_summary_sha256']!=digest(windows/'summary.json')):
        raise ValueError('延迟窗口与近实时目录身份不符')
    groups=defaultdict(list)
    for row in read(windows/'windows.json'):
        if row['split'] not in ('train','validation'):
            raise ValueError('近实时磁场输入超出授权划分')
        groups[row['uri']].append(row)
    if groups.keys()!=catalog['data'].keys():
        raise ValueError('近实时目录缺少或多出观测时间记录')
    outcomes=[]
    for uri,rows in groups.items():
        entry=catalog['data'][uri]
        if entry['state']=='available':
            nominal=FILENAME.fullmatch(Path(uri).name)[1]
            name=f'{nominal}.json'
            path=intake/'raw'/name
            if name!=entry['file'] or digest(path)!=entry['receipt']['sha256']:
                raise ValueError('近实时目录缓存已改变')
            stamp=entry['requested_time_tai']
            input_by_noaa=index_catalog(read(path),stamp)
            states=[attach(row,input_by_noaa[row['noaa']],stamp) for row in rows]
        else:
            states=[{'state':'catalog_empty_or_unavailable'} for _ in rows]
        for row,status in zip(rows,states,strict=True):
            outcomes.append({**{name:row[name] for name in ('uri','split','noaa','observed_utc','issue_utc','target_end_utc','target')},
                             **status})
    coverage={split:{'states':dict(Counter(r['state'] for r in outcomes if r['split']==split)),
                     'known_positive_states':dict(Counter(r['state'] for r in outcomes if r['split']==split and r['target']==1))}
              for split in ('train','validation')}
    fitted=evaluate(outcomes)
    plan={'windows_sha256':prior['windows_sha256'],'code_sha256':digest(__file__),
          'intake_sha256':digest(intake/'intake.json'),'quality_strata_exploratory':sorted(QUALITY),
          'timing':'Catalog record creation at or before the delayed issue cutoff. Historical first receipt remains unverified.',
          'cutoff_checked':True,'test_accessed':False,'calibration_accessed':False}
    atomic_json(output/'protocol.json',plan)
    atomic_json(output/'joined.json',outcomes)
    result={'state':'completed','created_utc':now(),'coverage':coverage,'paired_evaluation':fitted,
            'protocol_sha256':digest(output/'protocol.json'),'rows_sha256':digest(output/'joined.json'),
            'actual_historical_receipt_verified':False,'predictor_released':False,
            'test_accessed':False,'calibration_accessed':False}
    atomic_json(output/'summary.json',result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('dataset','windows','intake','output'):
        parser.add_argument('--'+name,required=True,type=Path)
    print(json.dumps(run(**{name:path.resolve() for name,path in vars(parser.parse_args()).items()}),ensure_ascii=False))


if __name__=='__main__':
    main()
