"""Learn to choose one H-alpha contour from several fixed probability levels."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from scipy.ndimage import label
from sklearn.ensemble import HistGradientBoostingClassifier
import torch

from .durable import atomic_json
from .fl_author_model import AuthorGraph
from .fl_direct_centers import architecture, infer, input_planes
from .fl_direct_reviewer import evidence, region_features
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT
from .fl_gong_fused_verify import later_pairs
from .fl_gong_patch_training import image_bytes
from .fl_gong_resolution import predict
from .fl_magfilo_audit import DATA_SHA256, raster
from .fl_magfilo_transfer import LABEL_SHA256, inventory
from .fl_visual_candidate_review import measure
from .journal import read
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


LEVELS = (.35, .50, .65)
OVERLAP_LIMIT = .65
ARMS = ('single_classifier', 'family_classifier')


def suppress(scores, overlap):
    """Greedy suppression on high intersection relative to the smaller footprint."""
    selected = []
    available = np.ones(len(scores), dtype=bool)
    for i in np.argsort(-np.asarray(scores), kind='stable'):
        if available[i]:
            selected.append(int(i))
            available[overlap[i] >= OVERLAP_LIMIT] = False
    return selected


def ownership_overlap(maps):
    counts = [int(group.max()) for group in maps]
    ends = np.r_[0, np.cumsum(counts)]
    result = np.zeros((ends[-1], ends[-1]), dtype=np.float32)
    sizes = [np.bincount(group.ravel(), minlength=n+1)[1:] for group,n in zip(maps,counts,strict=True)]
    for i,source in enumerate(maps):
        left = slice(ends[i], ends[i+1])
        for j in range(i+1,len(maps)):
            right = slice(ends[j],ends[j+1])
            n,m=counts[i],counts[j]
            paired = np.bincount((source*(m+1)+maps[j]).ravel(), minlength=(n+1)*(m+1)).reshape(n+1,m+1)
            normal = np.minimum(sizes[i][:,None],sizes[j][None,:])
            result[left,right] = paired[1:,1:] / np.maximum(1,normal)
            result[right,left] = result[left,right].T
    return result


def describe(raw, references, base, model):
    fields, valid, radius, gray = input_planes(raw)
    mask, center = infer(model,fields,valid)
    old = predict(base,gray[None])[0]>=.5
    records = [label(raster(r),structure=CONNECTIVITY)[0] for r in references]
    maps, per_level, data, levels = [],[],[],[]
    for cutoff in LEVELS:
        bit = mask>=cutoff
        objects,n = label(bit,structure=CONNECTIVITY)
        maps.append(objects)
        per_level.append(evidence(bit,records))
        data.extend([*features,cutoff] for features in region_features(objects,gray,mask,center,old,valid[::2,::2],radius))
        levels.extend([cutoff]*n)
    geometry = [{**variants[0], 'iou':[r for variant in variants for r in variant['iou']]}
                for variants in zip(*per_level,strict=True)]
    n=len(data)
    matches = np.stack([np.asarray(v['iou']).reshape(n,len(v['reference_ids'])).max(axis=1,initial=0.) for v in geometry])
    targets = np.where((matches>=.5).all(axis=0),1,np.where((matches==0).all(axis=0),0,-1)).tolist()
    return {'features':data,'levels':levels,'geometry':geometry,'overlap':ownership_overlap(maps),
            'targets':targets,'variants':len(records)}


def make_model(rows, dims):
    pairs=[(x[:dims],y,1. if row['variants']>=2 else .6) for row in rows
           for x,y in zip(row['features'],row['targets'],strict=True) if y>=0]
    x,y,w=map(np.asarray,zip(*pairs))
    known=np.bincount(y,minlength=2)
    if known.min()<30:
        raise ValueError('轮廓家族复核对照不足')
    network=HistGradientBoostingClassifier(max_iter=90,max_leaf_nodes=12,l2_regularization=2.,
                                          learning_rate=.065,early_stopping=False,random_state=2026)
    network.fit(x,y,sample_weight=w*len(y)/(2*known[y]))
    return network, known.tolist()


def select(row, indices, scores):
    return {'base_count':0,'count':len(indices),'scores':{'review':[float(scores[k]) for k in indices]},
            'geometry':[{**r,'iou':[r['iou'][k] for k in indices]} for r in row['geometry']]}


def analyze(rows, single, multiple):
    output={}
    for row in rows:
        x=np.asarray(row['features'])
        basic=single.predict_proba(x[:,:15])[:,1] if len(x) else []
        ranked=multiple.predict_proba(x)[:,1] if len(x) else []
        ids=[i for i,value in enumerate(row['levels']) if value==.5]
        keys={'single':(ids,basic),'single_classifier':(suppress(basic,row['overlap']),basic),
              'family_classifier':(suppress(ranked,row['overlap']),ranked)}
        # Serialize only candidate geometry used by each scorer and the unsuppressed ceiling.
        output[row['file']]={k:select(row,*values) for k,values in keys.items()}
        ceiling=lambda g,m: int((np.asarray(g['iou']).reshape(m,len(g['reference_ids'])).max(axis=0,initial=0)>=.5).sum())
        output[row['file']]['ceiling']={'family':sum(ceiling(g,len(x)) for g in row['geometry']),
            'single':sum(ceiling(g,len(ids)) for g in output[row['file']]['single']['geometry'])}
    return output


def cutoff(rows, minimum):
    events={}
    for i,row in enumerate(rows):
        for value in row['scores']['review']:
            events.setdefault(value,set()).add(i)
    previous=[(0,0)]*len(rows)
    matched=count=0
    best=None
    for c in sorted(events,reverse=True):
        for i in events[c]:
            frame=measure([rows[i]],'review',c)
            a,b=previous[i]
            matched+=frame['matched']-a
            count+=frame['predicted']-b
            previous[i]=(frame['matched'],frame['predicted'])
        if matched>=minimum and count:
            entry=(matched/count,matched,-count,c)
            if best is None or entry>best:
                best=entry
    return best[-1] if best else None


def run(source,release,early,development,later,direct,anchor,control,output):
    output=output_path(output,DATASET)
    if output.exists() or digest(source)!=DATA_SHA256 or digest(release)!=LABEL_SHA256:
        raise ValueError('新轮廓研究需要冻结标注和新目录')
    previous=read(control/'summary.json'); inputs=read(control/'protocol.json')
    direct_info=read(direct/'summary.json');w=direct/'direct.pt'
    single_fit=read(control/'fit-geometry.json')
    if (digest(control/'protocol.json')!=previous['protocol_sha256'] or digest(w)!=direct_info['model_sha256']
            or inputs['model_sha256']!=digest(w) or [v['file'] for v in single_fit]!=inputs['samples']['fit']
            or previous['test_accessed']):
        raise ValueError('基线权重或时间划分不符')
    _,early_ref=inventory(source,release)
    _,late_ref=later_pairs(source,release)
    phases={'fit':(early,early_ref),'development':(development,early_ref),'2019_2020':(later,late_ref)}
    atomic_json(output/'protocol.json',{'created_utc':now(),'code_sha256':digest(__file__),
        'model_sha256':digest(w),'control_sha256':digest(control/'summary.json'),
        'control_fit_sha256':digest(control/'fit-geometry.json'),'samples':inputs['samples'],
        'levels':LEVELS,'suppression_intersection_over_smaller':OVERLAP_LIMIT,
        'scope':'2017 thresholds maximize precision at or above the frozen single-level matched count. 2019–2020 receives unchanged settings.',
        'test_accessed':False,'fresh_days_accessed':False})
    base,net=AuthorGraph('batch'),architecture()
    base_path=anchor/'mixed_gong/last.pt'
    if digest(base_path)!=read(anchor/'summary.json')['results']['mixed_gong']['weights_sha256']:
        raise ValueError('轮廓对照的辅助整图权重不符')
    base.load_state_dict(load_checkpoint(base_path)['model'])
    actual=torch.load(w,map_location='cpu',weights_only=True)
    if actual['protocol_sha256']!=direct_info['protocol_sha256']:
        raise ValueError('直接模型内部训练协议不符')
    net.load_state_dict(actual['model'])
    cv2.setNumThreads(2)
    torch.set_num_threads(2)
    single,single_count=make_model(single_fit,15)
    first=read(control/'fit-metrics.json')
    if single_count!=[first['proxy_background'],first['strict_positive']]:
        raise ValueError('单轮廓训练计数未复现')
    data={}
    with exclusive(ROOT/'.mdva/training-gpu.lock'),torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.backends.cuda.matmul.allow_tf32=False
        torch.backends.cudnn.allow_tf32=False
        for model in (base,net):
            model.cuda().eval()
        usage=Resources()
        for phase,(folder,labels) in phases.items():
            lookup={r['file']:r for r in read(folder/'inventory.json')}
            rows=[]
            for i,name in enumerate(inputs['samples'][phase],1):
                item=lookup[name]
                refs=[labels[a['id']] for a in item['annotators']]
                rows.append({'file':name,**describe(image_bytes(folder,item),
                    [r.values() if isinstance(r,dict) else r for r in refs],base,net)})
                if i%12==0:
                    atomic_json(output/'status.json',{'phase':phase,'done':i,'total':len(inputs['samples'][phase]),
                                                         'resources':usage.sample()})
            if phase=='fit':
                multiple,counts=make_model(rows,16)
                atomic_json(output/'fit-metrics.json',{'both':counts,'single':single_count,'images':len(rows)})
                continue
            data[phase]=analyze(rows,single,multiple)
        resources=usage.sample()
    results={}
    for phase,rows in data.items():
        sets={k:[v[k] for v in rows.values()] for k in ('single',*ARMS)}
        comparison=measure(sets['single'],'review',previous['policy']['standalone']['learned'])
        if comparison!=previous['scores'][phase]['retention']['learned']:
            raise ValueError('冻结单轮廓回放未复现')
        if phase=='development':
            plan={k:cutoff(sets[k],comparison['matched']) for k in ARMS}
            atomic_json(output/'policy.json',plan)
        results[phase]={'single':comparison,
            'family':{k:measure(sets[k],'review',c) for k,c in plan.items() if c is not None},
            'oracle':{k:sum(v['ceiling'][k] for v in rows.values()) for k in ('family','single')},
            'family_after_suppression':{k:measure(sets[k],'all',0.) for k in ARMS}}
        atomic_json(output/f'{phase}-cases.json',rows)
    final={'state':'completed','created_utc':now(),'scores':results,'policy':plan,'resources':resources,
           'protocol_sha256':digest(output/'protocol.json'),'cases_sha256':{k:digest(output/f'{k}-cases.json') for k in results},
           'test_accessed':False,'deployed':False}
    atomic_json(output/'summary.json',final)
    atomic_json(output/'status.json',{'state':'completed','summary_sha256':digest(output/'summary.json')})
    return final


if __name__=='__main__':
    configure_environment()
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','release','early','development','later','direct','anchor','control','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    result=run(**vars(parser.parse_args()))
    print(json.dumps({'state':result['state'],'policy':result['policy']},ensure_ascii=False))
