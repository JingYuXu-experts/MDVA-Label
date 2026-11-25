"""Train-sample-only controlled study of the author model's outside-disk domain."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

from .datasets import SegmentationDataset
from .diagnostic_support import sample_id
from .durable import atomic_json
from .runtime import digest,now,output_path,exclusive,Resources

ROOT=Path(__file__).resolve().parents[3]
SOURCES=ROOT/'out/solar-model-sources/cnn193-browser-20261116'


def select(rows):
    if any(r['split']!='train' or r['category']!='CH' for r in rows):raise ValueError('仅允许CH训练样本')
    def area(row):return sum(int(v) for k,v in row['mask_statistics']['pixels'].items() if int(k)>0)
    ordered=sorted(rows,key=lambda r:(area(r),r['uri']))
    bins=np.array_split(np.arange(len(ordered)),3);rng=np.random.default_rng(2026)
    return [ordered[int(i)] for b in bins for i in sorted(rng.choice(b,size=min(10,len(b)),replace=False))]


def prepare(dataset,output):
    dataset=Path(dataset).resolve();output=output_path(output,dataset)
    if output.exists():raise ValueError('使用新的试验目录，禁止覆盖')
    data=SegmentationDataset(dataset,'CH','train');chosen=select(data.rows)
    audit=ROOT/'out/recognition-improvement-20261116/display-01'
    proto=json.loads((audit/'protocol.json').read_text(encoding='utf-8'))
    if proto['frozen_manifest_sha256']!=digest(dataset/'manifest-hashes.json'):
        raise ValueError('原输入审计数据身份不符')
    fit_code=Path(__file__).with_name('spatial_audit.py')
    if proto['code']['spatial_audit.py']!=digest(fit_code):raise ValueError('拟合代码改变')
    if proto['input_rows_sha256']!=digest(audit/'images.jsonl'):raise ValueError('输入拟合记录改变')
    measured={r['uri']:r for r in map(json.loads,(audit/'images.jsonl').read_text(encoding='utf-8').splitlines()) if r['split']=='train'}
    rows=[]
    for row in chosen:
        image=data.source(row['uri']);info=measured[row['uri']]
        if digest(image)!=info['image_sha256']:raise ValueError('输入审计图像改变')
        rows.append({'id':sample_id(row['uri']),'uri':row['uri'],'source':str(image),'source_sha256':digest(image),
                     'disk':info.get('disk') if info['available'] else None,'fallback':not info['available'],
                     'fallback_reason':info.get('reason'),'time_utc':row['time_utc']})
    protocol={'scope':'CH train 30 only','created_utc':now(),'seed':2026,'sample_rule':'10 per foreground-area rank tertile; shared fixed images for both arms',
        'rows':rows,'arms':['original','black_outside'],'binary_rule':'floor(linear+.5)==1',
        'transform':'Replace only pixels outside reliable image-only apparent disk with black; disk interior unchanged; unsupported fits unchanged.',
        'not_physical_calibration':True,'model_commit':'a73a848bbc7eb5ef012daa9373a83fd7ee8cfd9a',
        'gates_for_validation_feasibility_only':{'mean_dice':.6,'mean_precision':.7,'mean_recall':.5,'nonempty_images':24,'paired_dice_gain':.05},
        'preparation_sha256':digest(__file__),'fit_code_sha256':digest(fit_code),'manifest_sha256':digest(dataset/'manifest-hashes.json'),
        'audit_sha256':digest(audit/'images.jsonl'),'cache_script_sha256':digest(SOURCES/'paired-input-cache.cjs'),
        'training':False,'validation_accessed':False,'test_accessed':False,'calibration_accessed':False,
        'decision_scope':'Pilot feasibility only, never deployment or generalization. Author pretraining overlap with local training years is unknown.'}
    output.mkdir(parents=True);atomic_json(output/'protocol.json',protocol)
    return protocol


def run(dataset,output):
    import torch
    from .author_tfjs import load,native_binary
    from .metrics import binary_segmentation,segmentation_summary
    from .ch_display_probe import localization_record
    from .localization_audit import aggregate
    dataset=Path(dataset).resolve();output=output_path(output,dataset)
    protocol=json.loads((output/'protocol.json').read_text(encoding='utf-8'))
    if protocol['scope']!='CH train 30 only' or len(protocol['rows'])!=30 or protocol['preparation_sha256']!=digest(__file__):raise ValueError('非登记训练样本对照')
    if protocol['manifest_sha256']!=digest(dataset/'manifest-hashes.json') or protocol['cache_script_sha256']!=digest(SOURCES/'paired-input-cache.cjs'):raise ValueError('输入或代码身份改变')
    if (output/'identity.json').exists():raise ValueError('已运行，禁止静默覆盖')
    cache=json.loads((output/'inputs.json').read_text(encoding='utf-8'))
    if cache['protocol_sha256']!=digest(output/'protocol.json') or cache['external_requests']!=0 or len(cache['rows'])!=30:raise ValueError('输入缓存不完整或身份改变')
    data=SegmentationDataset(dataset,'CH','train');allowed={r['uri']:r for r in data.rows}
    if len({r['uri'] for r in protocol['rows']})!=30 or any(r['uri'] not in allowed for r in protocol['rows']):raise ValueError('样本不是冻结训练范围')
    identity={'protocol_sha256':digest(output/'protocol.json'),'cache_sha256':digest(output/'inputs.json'),
        'snapshot_sha256':digest(SOURCES/'keras/snapshot.json'),
        'code':{name:digest(Path(__file__).with_name(name)) for name in ('author_background_probe.py','author_tfjs.py','author_model_snapshot.py','ch_display_probe.py','localization_audit.py','metrics.py')}}
    atomic_json(output/'identity.json',identity)
    scores={a:[] for a in protocol['arms']};locations={a:[] for a in scores}
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
        model,_=load(SOURCES/'keras','cuda');resource=Resources()
        with torch.inference_mode():
            for i,(row,item) in enumerate(zip(protocol['rows'],cache['rows'])):
                if row['uri']!=item['uri']:raise ValueError('输入顺序改变')
                frozen=allowed[row['uri']];data.source(row['uri']);predictions={};ranges={}
                for arm in scores:
                    p=(output/item['inputs'][arm]['file']).resolve()
                    if not p.is_relative_to((output/'inputs').resolve()) or digest(p)!=item['inputs'][arm]['sha256']:raise ValueError('输入缓存路径或身份不符')
                    x=np.fromfile(p,dtype='<f4')
                    if x.size!=256**2 or not np.isfinite(x).all() or x.min()<0 or x.max()>1:raise ValueError('输入值异常')
                    raw=model(torch.from_numpy(x.reshape(1,1,256,256).copy()).cuda())[0,0].cpu().numpy()
                    predictions[arm]=np.asarray(Image.fromarray(native_binary(raw).astype('uint8')).resize((512,512),Image.Resampling.NEAREST))>0
                    ranges[arm]=[float(raw.min()),float(raw.max())]
                # No mask participates in selecting or applying the intervention.
                reference=np.load(data.source(frozen['mask_uri']),allow_pickle=False)
                folder=output/'cases'/row['id'];folder.mkdir(parents=True)
                record={'uri':row['uri'],'scores':{},'ranges':ranges,'input_fit_available':row['disk'] is not None}
                for arm,pred in predictions.items():
                    score=binary_segmentation(pred,reference>0,boundary=True);scores[arm].append(score)
                    loc=localization_record(frozen,pred,reference,score,arm);locations[arm].append(loc)
                    record['scores'][arm]=score
                    Image.fromarray(pred.astype('uint8')*255).save(folder/(arm+'.png'))
                atomic_json(folder/'result.json',record)
                atomic_json(output/'status.json',{'state':'inference_only','completed':i+1,'total':30,'training':False,'resource':resource.sample()})
    metrics={a:segmentation_summary(r) for a,r in scores.items()};black=metrics['black_outside'];limits=protocol['gates_for_validation_feasibility_only']
    nonempty=sum(r['predicted_pixels']>0 for r in scores['black_outside'])
    checks={'dice':black['dice']>=limits['mean_dice'],'precision':black['precision']>=limits['mean_precision'],
        'recall':black['recall']>=limits['mean_recall'],'nonempty':nonempty>=limits['nonempty_images'],
        'paired_gain':black['dice']>=metrics['original']['dice']+limits['paired_dice_gain']}
    summary={'state':'completed','created_utc':now(),'identity':identity,'metrics':metrics,
        'localization':{a:aggregate(r) for a,r in locations.items()},'checks':checks,'gate_passed':all(checks.values()),
        'black_nonempty':nonempty,'input_fit_available':sum(r['disk'] is not None for r in protocol['rows']),
        'decision':'validation_feasibility_only' if all(checks.values()) else 'background_alone_insufficient_do_not_expand',
        'training':False,'validation_accessed':False,'test_accessed':False,'calibration_accessed':False,'models_replaced':False,
        'resource':resource.sample(),'scope':'Training-sample input-domain pilot only. Does not demonstrate held-out scientific performance.'}
    atomic_json(output/'summary.json',summary);atomic_json(output/'status.json',{'state':'completed','completed':30,'total':30,'training':False})
    return summary


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment()
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','run']);p.add_argument('--dataset',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();r=prepare(a.dataset,a.output) if a.action=='prepare' else run(a.dataset,a.output)
    print(json.dumps({k:r[k] for k in ('scope','state','decision','metrics','checks') if k in r},ensure_ascii=False))
