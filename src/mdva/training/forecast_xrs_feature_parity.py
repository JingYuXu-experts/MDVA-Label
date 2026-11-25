"""Propagate paired operational/science GOES histories to model features and score shifts."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

import numpy as np
from scipy.special import expit

from .durable import atomic_json
from .forecast_disk_xrs import ARMS, XRS, history, read_minutes
from .forecast_xrs_intake import FEEDS, usable_minutes
from .journal import read
from .runtime import digest, now, output_path
from .xrs_coverage import read_source, utc


MODEL = 'xrs_plus_raw_plus_regions'


def score_delta(operational, science, coefficients):
    columns = coefficients['features']
    if columns!=list(ARMS[MODEL]):
        raise ValueError('研究预测的特征已改变')
    changes = {key:operational[key]-science[key] for key in XRS}
    logit_delta = 0.
    for k,v in changes.items():
        i=columns.index(k)
        scale, coef=coefficients['scale'][i],coefficients['coef'][i]
        if not np.isfinite([scale,coef]).all() or scale<=0:
            raise ValueError('预测模型尺度异常')
        logit_delta += v*coef/scale
    return {'delta_features':changes,'logit_delta_magnetogram_held_fixed':logit_delta,
            'max_possible_probability_change':float(expit(abs(logit_delta)/2)-expit(-abs(logit_delta)/2))}


def common_minutes(operational, science):
    _, live, archive = np.intersect1d(operational[0],science[0],return_indices=True)
    return operational[:,live],science[:,archive]


def run(dataset, snapshot, comparison, forecast, output):
    output = output_path(output,dataset)
    if output.exists() or any(output.is_relative_to(p.resolve()) for p in (snapshot,comparison,forecast)):
        raise ValueError('GOES特征对照需要独立目录')
    check, meta = read(comparison/'summary.json'), read(forecast/'summary.json')
    weights = forecast/'weights.json'
    if (check['state']!='completed' or check['snapshot_sha256']!=digest(snapshot/'summary.json')
            or check['issued_forecast'] or meta['weights_sha256']!=digest(weights) or meta['test_accessed']):
        raise ValueError('同源观测或预测权重身份不符')
    coefficient=read(weights)[MODEL]
    raw=read(snapshot/'summary.json')
    rows=[]
    for feed,table in check['feeds'].items():
        rec=raw['feeds'][feed]
        f=snapshot/(feed+'.json')
        if rec['url']!=FEEDS[feed] or digest(f)!=rec['raw_sha256']:
            raise ValueError('业务通量快照改变')
        received=utc(rec['received_utc'])
        operational,_,_=usable_minutes(read(f),received)
        all_science=defaultdict(list)
        for entry in table:
            if entry['state']!='compared':
                continue
            satellite=entry['satellite']
            elements=read_source(comparison,entry['science_source'])
            stamp,flux=read_minutes(comparison,elements)
            positive=np.isfinite(flux)&(flux>0)
            all_science[satellite].append((entry['day_utc'],np.stack((stamp[positive],flux[positive]))))
        for satellite, chunks in all_science.items():
            science=np.concatenate([keep for _,keep in sorted(chunks,key=lambda x:x[0])],axis=1)
            if not (np.diff(science[0])>0).all() or satellite not in operational:
                raise ValueError('星号、通量时间重复或次序错误')
            both_live,both_archive=common_minutes(operational[satellite],science)
            for stamp,_ in sorted(chunks,key=lambda x:x[0]):
                day=datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)
                for step in (0,6,12,18):
                    issue=day+timedelta(hours=step)
                    if issue>=received:
                        continue
                    features=[history(issue.isoformat(),{satellite:x}) for x in (operational[satellite],science,both_live,both_archive)]
                    data={'feed':feed,'satellite':satellite,'issue_utc':issue.isoformat(),'coverage':[p['valid_minutes_24h'] if p else None for p in features]}
                    for key,(a,b) in zip(('normal_quality_per_product','same_minute_support'),(features[:2],features[2:]),strict=True):
                        if a is not None and b is not None:
                            data[key]=score_delta(a['features'],b['features'],coefficient)
                    rows.append(data)
    groups={}
    for key in ('normal_quality_per_product','same_minute_support'):
        shifts=[r[key] for r in rows if key in r]
        if shifts:
            groups[key]={'windows':len(shifts),'max_abs_logit_change':max(abs(s['logit_delta_magnetogram_held_fixed']) for s in shifts),
                         'probability_shift_bound_max':max(s['max_possible_probability_change'] for s in shifts),
                         'logit_change_median_abs':float(np.median([abs(s['logit_delta_magnetogram_held_fixed']) for s in shifts]))}
    atomic_json(output/'windows.json',rows)
    result={'state':'completed','recorded_utc':now(),'candidate_windows':len(rows),'groups':groups,
            'snapshot_sha256':digest(snapshot/'summary.json'),'science_comparison_sha256':digest(comparison/'summary.json'),
            'forecast_coefficients_sha256':digest(weights),'windows_sha256':digest(output/'windows.json'),
            'scope':'retrospective input feature comparison; all earlier observations come from a later recorded operational snapshot',
            'test_accessed':False,'issued_forecast':False,'calibration_accessed':False}
    atomic_json(output/'summary.json',result)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('dataset','snapshot','comparison','forecast','output'):
        parser.add_argument('--'+key,type=Path,required=True)
    print(json.dumps(run(**vars(parser.parse_args())),ensure_ascii=False))
