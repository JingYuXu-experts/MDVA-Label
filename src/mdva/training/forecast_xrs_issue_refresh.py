"""Replay same-satellite GOES features available by a delayed issue-time cutoff."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import timedelta
import json
from pathlib import Path

import numpy as np
from sklearn.metrics import average_precision_score

from .durable import atomic_json
from .forecast_disk_receipt_delay import FEATURE_SETS, evaluate, paired_gain
from .forecast_disk_xrs import DELAY, MINUTE, MINUTES, history, series_for
from .journal import read
from .runtime import configure_environment, digest, now, output_path
from .xrs_coverage import utc


DELAYS = (70,120)
PRIMARY = 70


def paired_histories(row, issue, series):
    """Change only the time cutoff and preserve the original historical satellite."""
    observed = utc(row['observed_utc'])
    if issue < observed:
        raise ValueError('签发时刻早于磁图观测')
    sat = row['xrs']['satellite']
    if sat not in series:
        raise ValueError('原GOES卫星缺少经核验的档案')
    same = {sat:series[sat]}
    before = history(row['observed_utc'],same)
    if before is None:
        raise ValueError('原GOES历史不可复算')
    for k,v in row['xrs'].items():
        if before[k]!=v:
            raise ValueError('原GOES分钟范围或卫星不符')
    if any(not np.isclose(row['features'][k],v,atol=1e-10,rtol=1e-10) for k,v in before['features'].items()):
        raise ValueError('原GOES输入特征不一致')
    latest = history(issue.isoformat(),same)
    if latest is not None and utc(latest['last_minute_end_utc']) > issue-timedelta(seconds=DELAY):
        raise ValueError('更新的GOES分钟穿越签发前截点')
    return before,latest


def paired_refresh(rows, before, after, truth):
    stamps=np.asarray([int(utc(r['issue_utc']).timestamp()//(86400*27)) for r in rows])
    blocks=[np.flatnonzero(stamps==v) for v in np.unique(stamps)]
    random=np.random.default_rng(2026)
    deltas={key:[] for key in FEATURE_SETS}
    for _ in range(2000):
        chosen=np.concatenate([blocks[i] for i in random.integers(len(blocks),size=len(blocks))])
        y=truth[chosen]
        if np.unique(y).size<2:
            continue
        for key in deltas:
            deltas[key].append(average_precision_score(y,after[key][chosen])-average_precision_score(y,before[key][chosen]))
    if any(not v for v in deltas.values()):
        raise ValueError('没有可用于比较的正负时段')
    return {'blocks':len(blocks),'resamples':len(next(iter(deltas.values()))),
            'ap_change_ci95':{key:np.quantile(v,[.025,.975]).tolist() for key,v in deltas.items()}}


def active_rows(original, by_delay):
    rows={}
    for raw in original:
        by_offset={minutes:by_delay[str(minutes)][raw['uri']] for minutes in (0,*DELAYS)}
        if by_offset[0]['target']!=raw['target'] or utc(by_offset[0]['issue_utc'])!=utc(raw['observed_utc']):
            raise ValueError('复用的目标记录不能复算零延迟')
        for offset, expected in by_offset.items():
            issue=utc(expected['issue_utc'])
            if (issue-utc(raw['observed_utc'])!=timedelta(minutes=offset)
                    or utc(expected['target_end_utc'])-issue!=timedelta(hours=24)
                    or expected['split']!=raw['split']):
                raise ValueError('原目标窗或时间划分变化')
        if all(x['target'] in (0,1) for x in by_offset.values()):
            rows[raw['uri']]=by_offset
    dev_start=min(utc(r[0]['issue_utc']) for r in rows.values() if r[0]['split']=='validation')
    return {uri:r for uri,r in rows.items()
            if r[0]['split']=='validation' or utc(r[max(DELAYS)]['target_end_utc'])<dev_start}


def run(dataset, archived, delayed, audit, sources, output):
    output=output_path(output,dataset)
    if output.exists():
        raise ValueError('GOES签发时钟对照需独立目录')
    prior,old,official=(read(p/'summary.json') for p in (delayed,archived,audit))
    recipe=read(delayed/'protocol.json')
    if (any(v['state']!='completed' for v in (prior,old))
            or any(v.get('test_accessed') or v.get('calibration_accessed') for v in (prior,old))
            or digest(delayed/'protocol.json')!=prior['protocol_sha256']
            or digest(delayed/'windows.json')!=prior['windows_sha256']
            or digest(archived/'rows.json')!=old['rows_sha256'] or recipe['xrs_rows_sha256']!=old['rows_sha256']
            or digest(audit/'coverage-index.json')!=recipe['coverage_sha256']
            or recipe['coverage_sha256']!=official['coverage_index_sha256']
            or official['frozen_manifest_sha256']!=digest(dataset/'manifest-hashes.json')):
        raise ValueError('输入、历史目标窗或签发对照摘要不符')
    original=read(archived/'rows.json')
    by_delay={k:{r['uri']:r for r in items} for k,items in read(delayed/'windows.json').items()}
    if any(r['split'] not in ('train','validation') for r in original):
        raise ValueError('更新研究仅复用训练和开发观测')
    selected=active_rows(original,by_delay)
    expected=prior['result'][str(PRIMARY)]['cohort']
    if len(selected)!=expected['train']+expected['development']:
        raise ValueError('延迟研究的共同队列不能复现')
    start=min(utc(r[0]['issue_utc']).timestamp() for r in selected.values())-(MINUTES+16)*MINUTE
    stop=max(utc(r[max(DELAYS)]['issue_utc']).timestamp() for r in selected.values())
    minutes,receipts=series_for(read(audit/'coverage-index.json'),sources,start,stop)
    pairs={offset:{'stale':[],'refreshed':[]} for offset in DELAYS}
    missing=Counter()
    trace=[]
    for raw in original:
        if raw['uri'] not in selected:
            continue
        by_offset=selected[raw['uri']]
        current={offset:paired_histories(raw,utc(by_offset[offset]['issue_utc']),minutes)[1] for offset in DELAYS}
        if not all(current.values()):
            missing.update([raw['split']])
            continue
        recorded={'uri':raw['uri'],'split':raw['split'],'original_xrs':raw['xrs'],'issue_histories':{}}
        for offset,updated in current.items():
            shifted={**by_offset[offset],'features':raw['features']}
            pairs[offset]['stale'].append(shifted)
            pairs[offset]['refreshed'].append({**shifted,'features':{**shifted['features'],**updated['features']}})
            recorded['issue_histories'][offset]=updated
        trace.append(recorded)
    results={}
    predictions={}
    for offset,sets in pairs.items():
        scores_by_arm={}
        results[offset]={}
        for variant,rows in sets.items():
            comparison,scored,y,cohort=evaluate(rows,offset)
            dev=[r for r in rows if r['split']=='validation']
            scores_by_arm[variant]=scored
            results[offset][variant]={'cohort':cohort,'models':comparison,
                                      'region_increment':paired_gain(dev,scored,y)}
        results[offset]['refresh_increment']=paired_refresh(dev,scores_by_arm['stale'],scores_by_arm['refreshed'],y)
        predictions[offset]=[{'uri':r['uri'],'issue_utc':r['issue_utc'],'target':r['target'],
            'scores':{variant:{m:float(ps[i]) for m,ps in score.items()} for variant,score in scores_by_arm.items()}}
            for i,r in enumerate(dev)]
    protocol={'code_sha256':digest(__file__),'archived_result_sha256':digest(archived/'summary.json'),
        'delayed_result_sha256':digest(delayed/'summary.json'),'source_coverage_sha256':official['coverage_index_sha256'],
        'delays':DELAYS,'primary':PRIMARY,'history_margin_seconds':DELAY,'sources':receipts,
        'withdraw_on_missing_same_satellite_history':True,'test_accessed':False,'calibration_accessed':False,
        'scope':'earlier and updated history on the same satellite, identical labels/region inputs, archived quality records; actual historical minute receipts unknown'}
    atomic_json(output/'protocol.json',protocol)
    atomic_json(output/'histories.json',trace)
    atomic_json(output/'predictions.json',predictions)
    result={'state':'completed','created_utc':now(),'result':results,'excluded_for_refreshed_history':dict(missing),
            'histories_sha256':digest(output/'histories.json'),'protocol_sha256':digest(output/'protocol.json'),
            'predictions_sha256':digest(output/'predictions.json'),'issued_forecast':False,'test_accessed':False,
            'calibration_accessed':False,'operational_input_availability_verified':False}
    atomic_json(output/'summary.json',result)
    return result


if __name__=='__main__':
    configure_environment()
    parser=argparse.ArgumentParser(description=__doc__)
    for arg in ('dataset','archived','delayed','audit','sources','output'):
        parser.add_argument('--'+arg,type=Path,required=True)
    result=run(**vars(parser.parse_args()))
    print(json.dumps({'state':result['state'],'excluded_for_refreshed_history':result['excluded_for_refreshed_history']}))
