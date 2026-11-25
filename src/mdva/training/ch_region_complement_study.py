"""Train CH component additions and replacements using two frozen score maps."""

from __future__ import annotations

import argparse
from collections import Counter,defaultdict
import json
from pathlib import Path

import numpy as np
from PIL import Image
from safetensors.torch import load_file
from scipy.ndimage import binary_dilation,find_objects,label
import torch

from .ar_hmi_candidate_ranker import tally
from .ar_hmi_temporal_ranker import build_metrics,object_scores,time_partition
from .ar_scale_study import ROOT
from .ch_boundary_growth import candidate_masks,remove_small
from .ch_region_filter_review import CONNECTIVITY
from .datasets import SegmentationDataset
from .diagnostic_support import sample_id
from .durable import atomic_json
from .journal import append,read
from .reviewed_train import load_checkpoint,seed_all
from .run_locations import model_and_identity
from .runtime import Resources,configure_environment,digest,exclusive,now,output_path
from .scss_models import architecture as scss_architecture


SEED=2026
TRAIN_IMAGES=384
LEDGER=ROOT/'out/training-20261112-v2'
ACTS=('addition','replacement')
FEATURES=('log_area','fill','radius','mean_primary','mean_secondary','primary_q20','secondary_q20',
          'brightness_inside','context_minus_inside','primary_iou','log_size_ratio')
POLICY_PRECISION=.75


def geometry(raster,ref):
    n=int(raster.max())
    areas=np.bincount(raster.ravel(),minlength=n+1)[1:]
    base, matrix, _=object_scores(raster,[{'area':int(area)} for area in areas],ref,[True]*n)
    return base,matrix,areas


def score_geometry(base, additions, removed, select):
    combined={**base,'iou':[*base['iou'],*(additions[i]['iou'] for i in select)]}
    keep=[i not in removed for i in range(len(base['iou']))]+[True]*len(select)
    return build_metrics(combined,keep)[0]


def option_geometry(primary,secondary,primary_p,secondary_p,gray,reference):
    baseline,scores,primary_areas=geometry(primary,reference)
    _,alternate,sec_areas=geometry(secondary,reference)
    options=[]
    for j,window in enumerate(find_objects(secondary),1):
        y,x=window
        extended=(slice(max(0,y.start-1),min(512,y.stop+1)),slice(max(0,x.start-1),min(512,x.stop+1)))
        owns=secondary[window]==j
        local=secondary[extended]==j
        touch=np.unique(primary[extended][binary_dilation(local,structure=CONNECTIVITY)])
        touched=[int(i) for i in touch if i>0]
        if len(touched)>1:
            continue
        original=touched[0]-1 if touched else None
        size=int(sec_areas[j-1])
        shared=int(np.count_nonzero((primary[window]==original+1)&owns)) if original is not None else 0
        previous_size=int(primary_areas[original]) if original is not None else size
        if original is not None and shared/min(size,previous_size)<.20:
            continue
        p,q,brightness=(matrix[window][owns] for matrix in (primary_p,secondary_p,gray))
        nearby=gray[max(0,y.start-12):min(512,y.stop+12),max(0,x.start-12):min(512,x.stop+12)]
        values=[float(np.log1p(size)),size/owns.size,
                float(np.hypot((y.stop+y.start)/2-256,(x.stop+x.start)/2-256)/256),
                float(p.mean()),float(q.mean()),float(np.quantile(p,.20)),float(np.quantile(q,.20)),
                float(brightness.mean()/255),float((nearby.mean()-brightness.mean())/255),
                shared/(previous_size+size-shared),float(np.log(size/previous_size))]
        row={'kind':'replacement' if original is not None else 'addition','replaces':original,
             'source_component':j,'bbox_xyxy':[x.start,y.start,x.stop,y.stop],
             'features':values,'iou':alternate['iou'][j-1]}
        scored=score_geometry(scores,[row],{original} if original is not None else set(),[0])
        old,new=map(set,(baseline['matched_reference_ids'],scored['matched_reference_ids']))
        row.update(gained=len(new-old),lost=len(old-new),improves=bool(old<new))
        options.append(row)
    return baseline,scores,options


def collect(row,reference,gray,first,second):
    if not (gray.shape==first.shape==second.shape==reference.shape==(512,512)
            and np.isfinite(first).all() and np.isfinite(second).all()):
        raise ValueError('冕洞候选没有通过完整同图配对')
    base=label(candidate_masks(first)['bounded_growth'],structure=CONNECTIVITY)[0]
    supplementary=label(remove_small(second>=.5),structure=CONNECTIVITY)[0]
    actual=label(reference>0,structure=CONNECTIVITY)[0]
    baseline,precomputed,candidates=option_geometry(base,supplementary,first,second,gray,actual)
    scss=geometry(supplementary,actual)[0]
    return {'uri':row['uri'],'time_utc':row['time_utc'],'baseline':baseline,'scss':scss,
            'geometry':precomputed,'options':candidates}


def fit_models(data):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    roles=time_partition(data)
    models,cuts,status={},{},{}
    for action in ACTS:
        fit,policy=([x for image in roles[role] for x in image['options'] if x['kind']==action] for role in ('fit','policy'))
        observed=np.asarray([item['improves'] for item in fit],dtype=bool)
        expected=np.asarray([item['improves'] for item in policy],dtype=bool)
        record={'fit':len(fit),'fit_new_regions':int(observed.sum()),'threshold_pool':len(policy),
                'threshold_new_regions':int(expected.sum()),'state':'insufficient'}
        status[action]=record
        if min(observed.sum(),(~observed).sum())<15 or expected.sum()<8:
            continue
        model=make_pipeline(StandardScaler(),LogisticRegression(C=1,class_weight='balanced',random_state=SEED))
        model.fit([r['features'] for r in fit],observed)
        scores=model.decision_function([r['features'] for r in policy])
        feasible=[]
        for cut in np.unique(scores):
            selected=scores>=cut
            if selected.sum()>=8 and expected[selected].mean()>=POLICY_PRECISION:
                feasible.append((int(selected.sum()),float(cut)))
        if not feasible:
            record['state']='policy_precision_unmet'
            continue
        count,cut=max(feasible)
        scaler,logit=(value for _,value in model.steps)
        cuts[action]={'threshold':cut,'mean':scaler.mean_.tolist(),'scale':scaler.scale_.tolist(),
                      'weights':logit.coef_[0].tolist(),'intercept':float(logit.intercept_[0])}
        record.update(state='ready',policy_accept=count)
        models[action]=model
    return models,cuts,status,roles['held']


def non_conflicting(rows,ordered):
    replacements, accepted=set(),[]
    for j in ordered:
        original=rows[j]['replaces']
        if original is not None:
            if original in replacements:
                continue
            replacements.add(original)
        accepted.append(j)
    return replacements,accepted


def apply(row,models,thresholds):
    proposals=row['options']
    candidates=[]
    for index,option in enumerate(proposals):
        action=option['kind']
        if action not in models:
            continue
        certainty=float(models[action].decision_function([option['features']])[0])
        margin=certainty-thresholds[action]['threshold']
        if margin>=0:
            candidates.append((index,margin))
    trained_order=[i for i,_ in sorted(candidates,key=lambda x:(-x[1],x[0]))]
    labels_order=sorted((i for i,r in enumerate(proposals) if r['improves']),key=lambda i:(-proposals[i]['gained'],i))
    arms={}
    records={}
    for tag,order in (('learned',trained_order),('reference_selected',labels_order)):
        removed,active=non_conflicting(proposals,order)
        arms[tag]=score_geometry(row['geometry'],proposals,removed,active)
        records[tag]=Counter(proposals[i]['kind'] for i in active)
    return {**row,'objects':{'control':row['baseline'],'scss':row['scss'],**arms},'used':records}


def package(path):
    card=read(path/'model.json')
    weights=path/'weights.safetensors'
    constructor,identity=model_and_identity(card['config'])
    if (card['id']!='CH-C2-s2026' or card['threshold']!=.5
            or card['weights_sha256']!=digest(weights) or card['training_code']!=identity()):
        raise ValueError('冕洞原模型权重或训练身份不符')
    model,_=constructor(card['config'])
    model.load_state_dict(load_file(str(weights)))
    return model,card['weights_sha256']


def model_pair(reference,study,review):
    receipt,identity=(read(review/k) for k in ('summary.json','identity.json'))
    protocol=read(study/'protocol.json')
    path=review/'checkpoint.pt'
    if (receipt['state']!='completed' or receipt['test_accessed'] or receipt['calibration_accessed']
            or not receipt['exact_replay'] or receipt['identity']!=identity
            or identity['checkpoint_sha256']!=digest(path)
            or identity['training_protocol_sha256']!=digest(study/'protocol.json')):
        raise ValueError('SCSS必须使用固定的研究检查点')
    second,_=scss_architecture(protocol['config'])
    saved=load_checkpoint(path)
    if saved['identity']!=read(study/'run/identity.json') or saved['epoch']!=receipt['checkpoint_epoch']:
        raise ValueError('SCSS检查点来源不一致')
    second.load_state_dict(saved['model'])
    first,sha=package(reference)
    return first,second,{'primary':sha,'secondary':digest(path)}


def probabilities(model,x):
    value=model(x)
    if not torch.isfinite(value).all():
        raise ValueError('冻结模型的结果含非有限值')
    return value.float().sigmoid()[0,0].cpu().numpy()


def collect_train(folder,dataset,models,resource):
    rows=[]
    for i,row in enumerate(dataset.rows):
        record=folder/f'{i:04d}.json'
        stored=read(record)
        if stored is None:
            x=dataset[i]['image'][None].cuda()
            p,q=(probabilities(model,x) for model in models)
            truth=np.load(dataset.source(row['mask_uri']),allow_pickle=False)
            with Image.open(dataset.source(row['uri'])) as source:
                gray=np.array(source.convert('L'))
            stored=collect(row,truth,gray,p,q)
            atomic_json(record,stored)
        if stored['uri']!=row['uri']:
            raise ValueError('训练候选与缓存身份不符')
        rows.append(stored)
        if (i+1)%24==0:
            atomic_json(folder.parent/'progress.json',{'state':'collecting','done':i+1,'total':len(dataset)})
            resource.sample()
    return rows


def collect_dev(folder,dataset,diagnostics,review):
    index={r['uri']:r for r in read(diagnostics/'case-index.json') if r['run_id']=='CH-C2-s2026'
           and r['diagnostic_subset']=='validation'}
    identity=digest(review/'identity.json')
    if set(index)!={r['uri'] for r in dataset.rows} or read(diagnostics/'identity.json')['test_accessed']:
        raise ValueError('不能使用非开发诊断图或不同清单')
    for i,row in enumerate(dataset.rows):
        stored=folder/f'{i:04d}.json'
        prior=read(stored)
        if prior is not None:
            if prior['uri']!=row['uri']:
                raise ValueError('开发图与缓存错位')
            yield prior
            continue
        item=index[row['uri']]
        a=diagnostics/'probabilities/CH-C2-s2026'/(item['sample_id']+'.npy')
        case=review/'cases'/sample_id(row['uri'])
        b=case/'probability.npz'
        recorded=read(case/'result.json')
        if (item['probability_sha256']!=digest(a) or item['mask_uri']!=row['mask_uri']
                or recorded['identity_sha256']!=identity or recorded['assets']['probability.npz']!=digest(b)):
            raise ValueError('开发概率图内容或身份不一致')
        with np.load(b,allow_pickle=False) as source:
            second=source['probability']
        truth=np.load(dataset.source(row['mask_uri']),allow_pickle=False)
        with Image.open(dataset.source(row['uri'])) as source:
            gray=np.array(source.convert('L'))
        result=collect(row,truth,gray,np.load(a,allow_pickle=False),second)
        atomic_json(stored,result)
        if (i+1)%32==0:
            atomic_json(folder.parent/'progress.json',{'state':'development','done':i+1,'total':len(dataset)})
        yield result


def run(dataset,reference,study,review,diagnostics,previous,output):
    output=output_path(output,dataset)
    if (output/'summary.json').is_file() or any(output.is_relative_to(path) for path in (reference,study,review,diagnostics,previous)):
        raise ValueError('使用独立的冕洞研究目录')
    train,dev=(SegmentationDataset(dataset,'CH',role,512) for role in ('train','validation'))
    choice=sorted(train.rows,key=lambda r:(r['time_utc'],r['uri']))
    rng=np.random.default_rng(SEED)
    train.rows=[choice[int(i)] for group in np.array_split(np.arange(len(choice)),24)
                for i in sorted(rng.choice(group,size=TRAIN_IMAGES//24,replace=False))]
    earlier=read(previous/'summary.json')
    if earlier['state']!='completed' or earlier['test_accessed'] or earlier['images_sha256']!=digest(previous/'images.json'):
        raise ValueError('旧的区域阈值未能重播')
    models= model_pair(reference,study,review)
    plan={'weights':models[-1],'selected':[r['uri'] for r in train.rows],
          'code_sha256':digest(__file__),'manifest_sha256':digest(dataset/'manifest-hashes.json'),
          'old_boundaries_sha256':digest(previous/'summary.json'),'diagnostic_sha256':digest(diagnostics/'case-index.json'),
          'review_sha256':digest(review/'summary.json'),'policy_minimum_precision':POLICY_PRECISION,
          'test_accessed':False,'calibration_accessed':False}
    marker=output/'protocol.json'
    if marker.exists() and read(marker)!=plan:
        raise ValueError('恢复前对照、样本或代码改变')
    atomic_json(marker,plan)
    append(LEDGER,'development','冕洞候选增补与轮廓复核','使用独立训练期拟合，完整验证期比较真实候选匹配。')
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.backends.cudnn.allow_tf32=False
        torch.backends.cuda.matmul.allow_tf32=False
        seed_all(SEED)
        resource=Resources()
        with torch.inference_mode():
            built=collect_train(output/'train',train,[model.cuda().eval() for model in models[:2]],resource)
        usage=resource.sample()
    ranks,cuts,fit,held=fit_models(built)
    atomic_json(output/'rules.json',{'states':fit,'parameters':cuts})
    results={}
    for name,source in (('train_late_holdout',held),('development',collect_dev(output/'validation',dev,diagnostics,review))):
        rows=[apply(row,ranks,cuts) for row in source]
        groups={}
        for arm in ('control','scss','reference_selected','learned'):
            groups[arm]=tally(rows,arm)
        details=defaultdict(Counter)
        for row in rows:
            for action in row['options']:
                details[action['kind']].update(proposed=1,reference_improving=int(action['improves']))
        results[name]={'images':len(rows),'objects':groups,'options':details,
                       'learned_actions':dict(sum((Counter(row['used']['learned']) for row in rows),Counter())),
                       'reference_selected_actions':dict(sum((Counter(row['used']['reference_selected']) for row in rows),Counter()))}
        atomic_json(output/f'{name}.json',rows)
    old=earlier['results']['all/bounded_growth']
    current=results['development']['objects']['control']
    if (current['reference'],current['predicted'],current['matched'])!=(old['references'],old['predicted'],old['matched']):
        raise ValueError('冻结的原边界增长对象结果没有复现')
    summary={'state':'completed','created_utc':now(),'results':results,'fits':fit,
             'protocol_sha256':digest(marker),'rules_sha256':digest(output/'rules.json'),
             'development_sha256':digest(output/'development.json'),'resource':usage,'test_accessed':False,
             'calibration_accessed':False,'deployed':False}
    atomic_json(output/'summary.json',summary)
    atomic_json(output/'progress.json',{'state':'completed','done':len(dev),'total':len(dev)})
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('dataset','reference','study','review','diagnostics','previous','output'):
        parser.add_argument('--'+name,required=True,type=Path)
    configure_environment()
    found=run(**{key:path.resolve() for key,path in vars(parser.parse_args()).items()})
    print(json.dumps({key:found[key] for key in ('state','results','fits')},ensure_ascii=False))


if __name__=='__main__':
    main()
