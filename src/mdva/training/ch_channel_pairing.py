"""Audit existing CH labels against 193/211 nominal clocks; never stack by date."""
from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re

import numpy as np

from .datasets import SegmentationDataset
from .durable import atomic_json
from .runtime import digest,now,output_path


def filename_time(name,channel):
    match=re.fullmatch(rf'AIA(\d{{8}})_(\d{{4}})_0{channel}\.jpg',name)
    if not match:raise ValueError('非登记的AIA图像文件名')
    return datetime.strptime(''.join(match.groups()),'%Y%m%d%H%M').replace(tzinfo=timezone.utc)


def nearest(times,target):
    if not times:return None
    i=bisect_left(times,target)
    candidates=times[max(0,i-1):min(len(times),i+1)]
    # Past wins exact ties; explicitly keep signed time difference for callers.
    return min(candidates,key=lambda t:(abs((t-target).total_seconds()),t>target))


def run(dataset,output):
    dataset=Path(dataset).resolve();output=output_path(output,dataset)
    if output.exists() and any(output.iterdir()):raise ValueError('使用新的审计目录，不覆盖旧结果')
    output.mkdir(parents=True,exist_ok=True)
    roots=json.loads((dataset/'sources.json').read_text(encoding='utf-8'));source=Path(roots['solar_obs']).resolve()
    inventory={};duplicates=[];unparsed=[];inventory_rows=[]
    for channel in ('193','211'):
        grouped=defaultdict(list)
        for file in sorted((source/'Coronal EUV images'/f'SDO_AIA_{channel}').glob('*.jpg')):
            if not file.resolve().is_relative_to(source):raise ValueError('数据源链接越界')
            try:stamp=filename_time(file.name,channel)
            except ValueError:unparsed.append(file.relative_to(source).as_posix());continue
            stat=file.stat();uri='@solar_obs/'+file.relative_to(source).as_posix()
            grouped[stamp].append(uri)
            inventory_rows.append({'uri':uri,'nominal_time_utc':stamp.isoformat(),'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns})
        duplicates.extend({'channel':channel,'time':t.isoformat(),'uris':names} for t,names in grouped.items() if len(names)!=1)
        inventory[channel]={t:names[0] for t,names in grouped.items() if len(names)==1}
    common=sorted(set(inventory['193']) & set(inventory['211']));rows=[];summaries={}
    for split in ('train','validation'):
        data=SegmentationDataset(dataset,'CH',split)
        for row in data.rows:
            file=data.source(row['uri']);target=filename_time(file.name,'193');pair=nearest(common,target)
            entries={'uri':row['uri'],'split':split,'manifest_time_utc':row['time_utc'],'label_nominal_time_utc':target.isoformat(),
                'exact_193':target in inventory['193'],'exact_211':target in inventory['211'],'exact_both':target in common,
                'geometry_verified':False,'input_file_sha256':digest(file)}
            if pair is not None:
                entries.update(nearest_pair_time_utc=pair.isoformat(),signed_delta_minutes=(pair-target).total_seconds()/60,
                    nearest_193_uri=inventory['193'][pair],nearest_211_uri=inventory['211'][pair])
            rows.append(entries)
        selected=[r for r in rows if r['split']==split];deltas=[abs(r['signed_delta_minutes']) for r in selected if 'signed_delta_minutes' in r]
        summaries[split]={'images':len(selected),'exact_193':sum(r['exact_193'] for r in selected),
            'exact_211':sum(r['exact_211'] for r in selected),'exact_both':sum(r['exact_both'] for r in selected),
            'nearest_pair_in_future':sum(r.get('signed_delta_minutes',0)>0 for r in selected),
            'median_absolute_delta_minutes':float(np.median(deltas)) if deltas else None,
            'within_minutes':{str(limit):sum(d<=limit for d in deltas) for limit in (0,1,5,30,60,180,720,1440)}}
    inventory_sha=hashlib.sha256(json.dumps(inventory_rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    atomic_json(output/'pairs.json',rows);atomic_json(output/'inventory.json',inventory_rows)
    report={'state':'completed','updated_utc':now(),'scope':'CH train/validation only; observation file metadata, no reference masks',
        'manifest_sha256':digest(dataset/'manifest-hashes.json'),'evaluator_sha256':digest(__file__),
        'inventory_sha256':inventory_sha,'pairs_sha256':digest(output/'pairs.json'),'summaries':summaries,
        'observation_files':{c:len(inventory[c]) for c in inventory},'common_nominal_clocks':len(common),
        'duplicates':duplicates,'unparsed':unparsed,'pixel_alignment_verified':False,'training_started':False,
        'decision':'do_not_stack_daily_images_or_start_multichannel_training',
        'limitations':['Times are parsed nominal filename clocks, interpreted as UTC; no FITS header time validation.',
            'Equal names do not prove identical preprocessing, instrument registration or pointing.',
            'Nearest future observations must not be used as inputs available at an earlier forecast time.',
            'This is a cadence/product mismatch within already downloaded datasets, not evidence of an incomplete user download.']}
    atomic_json(output/'summary.json',report);return report


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment()
    p=argparse.ArgumentParser();p.add_argument('--dataset',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();r=run(a.dataset,a.output);print(json.dumps(r['summaries'],ensure_ascii=False))
