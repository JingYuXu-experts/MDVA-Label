"""Validation-only audit of coarse localization, using already frozen probabilities."""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path

import numpy as np
from scipy import ndimage
from scipy.optimize import linear_sum_assignment

from .durable import atomic_json
from .runtime import digest,now

MODELS=('AR-A0-s42','CH-C2-s2026')


def coverage(prediction,mask,instances=False):
    labels=mask if instances else ndimage.label(mask>0,structure=np.ones((3,3)))[0]
    ids,counts=np.unique(labels[labels>0],return_counts=True)
    hits={int(i):int(n) for i,n in zip(*np.unique(labels[prediction & (labels>0)],return_counts=True))}
    reference=[hits.get(int(i),0)/int(n) for i,n in zip(ids,counts)]
    pred_labels,n=ndimage.label(prediction,structure=np.ones((3,3)))
    sizes=np.bincount(pred_labels.ravel(),minlength=n+1)
    supported=np.bincount(pred_labels[mask>0],minlength=n+1)
    return reference,[float(supported[i]/sizes[i]) for i in range(1,n+1)]


def objects(mask,instances=False):
    if instances:
        ids=np.unique(mask);ids=ids[ids>0]
        # Preserve disconnected pieces bearing the same reference identity.
        regions=[mask==i for i in ids]
    else:
        labels,n=ndimage.label(mask>0,structure=np.ones((3,3)))
        regions=[labels==i for i in range(1,n+1)]
    result=[]
    for region in regions:
        y,x=np.nonzero(region)
        result.append({'bbox':[int(x.min()),int(y.min()),int(x.max()-x.min()+1),int(y.max()-y.min()+1)],
                       'pixels':len(x)})
    return result


def match_boxes(predicted,reference,threshold):
    if not 0<threshold<=1:raise ValueError('Invalid box IoU threshold')
    ious=np.zeros((len(predicted),len(reference)))
    for i,p in enumerate(predicted):
        x,y,w,h=p['bbox']
        for j,r in enumerate(reference):
            a,b,c,d=r['bbox']
            intersection=max(0,min(x+w,a+c)-max(x,a))*max(0,min(y+h,b+d)-max(y,b))
            ious[i,j]=intersection/(w*h+c*d-intersection)
    pairs=[]
    if ious.size:
        # Maximize valid one-to-one match count first, IoU second.
        reward=(ious>=threshold)*(min(ious.shape)+1)+ious
        a,b=linear_sum_assignment(-reward)
        pairs=[{'prediction':int(i),'reference':int(j),'iou':float(ious[i,j])}
               for i,j in zip(a,b) if ious[i,j]>=threshold]
    return {'matched':len(pairs),'predicted':len(predicted),'reference':len(reference),
            'false_positive':len(predicted)-len(pairs),'missed':len(reference)-len(pairs),'pairs':pairs}


def aggregate(rows):
    output={'images':len(rows),'dice_mean':float(np.mean([r['dice'] for r in rows]))}
    for threshold in ('0.3','0.5'):
        results=[r['matches'][threshold] for r in rows]
        totals={k:sum(v[k] for v in results) for k in ('matched','predicted','reference','false_positive','missed')}
        totals['precision']=totals['matched']/totals['predicted'] if totals['predicted'] else None
        totals['recall']=totals['matched']/totals['reference'] if totals['reference'] else None
        totals['false_positives_per_image']=totals['false_positive']/len(rows)
        output['box_iou_'+threshold]=totals
    output['size_recall_at_box_iou_0.5']={}
    for size in ('small_lt64','medium_64_511','large_ge512'):
        n=sum(sum(v['size_bin']==size for v in r['reference']) for r in rows)
        matched=sum(sum(r['reference'][p['reference']]['size_bin']==size for p in r['matches']['0.5']['pairs']) for r in rows)
        output['size_recall_at_box_iou_0.5'][size]={'reference':n,'matched':matched,'recall':matched/n if n else None}
    fractions=[v['covered_fraction'] for r in rows for v in r['reference']]
    support=[v['reference_overlap_fraction'] for r in rows for v in r['predicted']]
    output['reference_coverage']={'reference':len(fractions),
        'zero_overlap':sum(f==0 for f in fractions),
        'at_least_half_covered':sum(f>=.5 for f in fractions),
        'half_covered_fraction':float(np.mean(np.array(fractions)>=.5)) if fractions else None}
    output['predicted_components_without_reference_overlap']=sum(f==0 for f in support)
    return output


def run(report,output):
    from .datasets import read_frozen,resolve_uri
    report,output=Path(report).resolve(),Path(output).resolve()
    identity=json.loads((report/'identity.json').read_text(encoding='utf-8'))
    experiment=Path(identity['experiment'])
    protocol=json.loads((experiment/'protocol.json').read_text(encoding='utf-8'))
    dataset=Path(protocol['dataset']).resolve()
    from .runtime import output_path
    output=output_path(output,dataset)
    if output.is_relative_to(report) or output.is_relative_to(dataset):raise ValueError('输出不得写入诊断快照或冻结数据')
    if output.exists() and any(output.iterdir()):raise ValueError('输出已存在，请使用新版本')
    allowed={r['uri']:r for r in read_frozen(dataset,'segmentation-ready.jsonl') if r['eligible'] and r['split']=='validation'}
    fingerprints={r['uri']:r['sha256'] for r in read_frozen(dataset,'source-files.jsonl')}
    sources=json.loads((dataset/'sources.json').read_text(encoding='utf-8'))
    index=json.loads((report/'case-index.json').read_text(encoding='utf-8'))
    expected={r['id']:r['checkpoint_sha256'] for r in identity['checkpoints']}
    groups=defaultdict(list);seen=set()
    for row in index:
        if row['run_id'] not in MODELS or row['diagnostic_subset']!='validation':continue
        key=(row['run_id'],row['uri'])
        if key in seen:raise ValueError('重复的模型图像记录')
        seen.add(key)
        frozen=allowed.get(row['uri'])
        if not frozen or row['split']!='validation' or row['mask_uri']!=frozen['mask_uri']:raise ValueError('只允许冻结验证资料')
        if row['checkpoint_sha256']!=expected[row['run_id']]:raise ValueError('检查点身份不符')
        if not len(row['sample_id'])==24 or any(c not in '0123456789abcdef' for c in row['sample_id']):raise ValueError('样本编号不符')
        probability=report/'probabilities'/row['run_id']/(row['sample_id']+'.npy')
        if digest(probability)!=row['probability_sha256']:raise ValueError('概率缓存身份不符')
        for uri in (row['uri'],row['mask_uri']):
            if digest(resolve_uri(uri,sources))!=fingerprints[uri]:raise ValueError('原始资料身份不符')
        mask=np.load(resolve_uri(row['mask_uri'],sources),allow_pickle=False)
        p=np.load(probability,allow_pickle=False)
        if p.shape!=mask.shape or not np.isfinite(p).all() or (p<0).any() or (p>1).any():raise ValueError('概率或几何异常')
        predicted=objects(p>=.5);reference=objects(mask,instances=row['category']=='AR')
        covered,supported=coverage(p>=.5,mask,instances=row['category']=='AR')
        for obj,fraction in zip(reference,covered):obj['covered_fraction']=fraction
        for obj,fraction in zip(predicted,supported):obj['reference_overlap_fraction']=fraction
        for region in reference:
            scaled=region['pixels']*512**2/mask.size
            region['size_bin']='small_lt64' if scaled<64 else 'medium_64_511' if scaled<512 else 'large_ge512'
        record={k:row[k] for k in ('run_id','category','sample_id','uri','instrument','year','dice')}
        record.update(reference=reference,predicted=predicted,
            matches={str(t):match_boxes(predicted,reference,t) for t in (.3,.5)})
        groups[row['run_id']].append(record)
        if len(seen)%100==0:print(f'Completed {len(seen)} validation images',flush=True)
    for model in MODELS:
        if len(groups[model])!=sum(r['category']==model[:2] for r in allowed.values()):raise ValueError('完整验证集缺失')
    summary={model:aggregate(rows) for model,rows in groups.items()}
    result={'state':'completed','created_utc':now(),'scope':'complete_validation_only',
        'report_sha256':digest(report/'report.json'),'case_index_sha256':digest(report/'case-index.json'),
        'audit_code_sha256':digest(__file__),'threshold':.5,'models':summary,
        'limitations':['验证集已反复用于开发，不是独立性能结论','框IoU只衡量大致位置，不能替代精细分割',
            '预测是8连通区域；AR参考保持实例编号，CH参考为8连通区域；均不自动赋予NOAA身份',
            '没有独立无目标图像，不能测得独立无目标误报率','面积分层是归一化像素数，不是物理面积']}
    atomic_json(output/'summary.json',result)
    atomic_json(output/'cases.json',[row for rows in groups.values() for row in rows])
    atomic_json(output/'by-stratum.json',{model:{key:{v:aggregate([r for r in rows if r[key]==v])
        for v in sorted({r[key] for r in rows})} for key in ('instrument','year')} for model,rows in groups.items()})
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--report',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();print(json.dumps(run(args.report,args.output),ensure_ascii=True))
