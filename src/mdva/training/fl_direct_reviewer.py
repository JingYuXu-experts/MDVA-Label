"""Review directly predicted H-alpha regions using time-isolated supervision."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path

import cv2
import numpy as np
from scipy.ndimage import binary_dilation, binary_erosion, distance_transform_edt, find_objects, label
from sklearn.ensemble import HistGradientBoostingClassifier
import torch

from .ar_hmi_temporal_ranker import object_scores
from .durable import atomic_json
from .fl_author_model import AuthorGraph
from .fl_direct_centers import architecture, infer, input_planes
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT
from .fl_gong_fused_verify import later_pairs
from .fl_gong_patch_training import image_bytes
from .fl_gong_resolution import predict
from .fl_magfilo_audit import DATA_SHA256, raster
from .fl_magfilo_transfer import LABEL_SHA256, inventory
from .fl_visual_candidate_review import exact_policy, measure
from .journal import read
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


PRIMARY = .5
RETENTION = .90
ARMS = ('centers', 'contour', 'learned')


def region_features(objects, gray, contour, center, old, valid, radius):
    features = []
    for ident, (y, x) in enumerate(find_objects(objects), 1):
        window = (slice(max(0,y.start-10), min(y.stop+10,512)), slice(max(0,x.start-10), min(x.stop+10,512)))
        local = objects[window]
        part = local == ident
        support = valid[window]
        pixels = gray[window]
        neighborhood = binary_dilation(part,iterations=8) & ~binary_dilation(part,iterations=2) & support & (local==0)
        within = pixels[part]
        rim = pixels[neighborhood]
        area = int(part.sum())
        coordinates = np.argwhere(part).astype(float)
        mean = coordinates.mean(axis=0)
        pos = mean + [w.start for w in window]
        centered = coordinates - mean
        width, length = np.linalg.eigvalsh(centered.T @ centered / area)
        depth = distance_transform_edt(np.pad(part,1))[1:-1,1:-1][part]
        shadow = float(np.median(rim)-np.median(within)) if rim.size else 0.
        rim_range = float(np.quantile(rim,.8)-np.quantile(rim,.2)) if rim.size else 0.
        confidence = contour[window][part]
        peak = center[window][part]
        features.append([float(confidence.mean()),float(peak.max()),float(np.quantile(confidence,.2)),
            float(np.log1p(area)),float(np.hypot(*(pos-255.5))/radius),float(np.log((length+.5)/(width+.5))),
            area/((y.stop-y.start)*(x.stop-x.start)),
            float(1-binary_erosion(part).sum()/area),float(np.quantile(depth,.8)),shadow,rim_range,
            shadow/max(rim_range,.025),float(within.std()),float(np.mean(old[window][part])),float(support[part].mean())])
    return features


def evidence(marked, references):
    objects, count = label(marked, structure=CONNECTIVITY)
    sizes = [{'area':int(n)} for n in np.bincount(objects.ravel(),minlength=count+1)[1:]]
    return [object_scores(objects,sizes,r,[True]*count)[1] for r in references]


def material(raw, reference, whole, network):
    fields, valid, radius, gray = input_planes(raw)
    contour, center = infer(network,fields,valid)
    old = predict(whole,gray[None])[0]>=PRIMARY
    binary = contour>=PRIMARY
    objects, size = label(binary,structure=CONNECTIVITY)
    ref_parts = [label(raster(r),structure=CONNECTIVITY)[0] for r in reference]
    alone = evidence(binary,ref_parts)
    values = region_features(objects,gray,contour,center,old,valid[::2,::2],radius)
    maxima = np.stack([np.asarray(g['iou']).reshape(size,len(g['reference_ids'])).max(axis=1,initial=0.) for g in alone])
    state = np.where((maxima>=.5).all(axis=0),1,np.where((maxima==0).all(axis=0),0,-1))
    excluded = np.unique(objects[binary_dilation(old,structure=CONNECTIVITY,iterations=2)])
    safe = (objects>0) & ~np.isin(objects,excluded)
    combined = old | safe
    pieces, count = label(combined,structure=CONNECTIVITY)
    # Reorder regions to baseline first, so the common policy code never removes baseline detections.
    base, n = label(old,structure=CONNECTIVITY)
    extra, k = label(safe,structure=CONNECTIVITY)
    joined = np.where(safe,n+extra,base)
    if count != n+k or not np.array_equal(pieces>0,joined>0):
        raise ValueError('新增候选没有与整图参考正确隔开')
    index = [int(objects[extra==i][0])-1 for i in range(1,k+1)]
    sizes = [{'area':int(v)} for v in np.bincount(joined.ravel(),minlength=n+k+1)[1:]]
    supplement = [object_scores(joined,sizes,ref,[True]*(n+k))[1] for ref in ref_parts]
    return {'features':values,'targets':state.tolist(),'variants':len(ref_parts),
            'standalone':{'base_count':0,'count':size,'geometry':alone},
            'additional':{'base_count':n,'count':k,'geometry':supplement},'additional_indices':index}


def rank(rows, model):
    for row in rows:
        x = np.asarray(row['features'],dtype=float)
        scores = {'contour':x[:,0].tolist(),'centers':x[:,1].tolist(),
                  'learned':model.predict_proba(x)[:,1].tolist()} if x.size else {name:[] for name in ARMS}
        row['standalone']['scores']=scores
        row['additional']['scores']={name:[v[i] for i in row['additional_indices']] for name,v in scores.items()}


def select_retained(rows):
    """Choose the most precise operating point retaining 90% of development matches."""
    maximum = measure(rows,'all',0.)['matched']
    choices = {}
    for arm in ARMS:
        # Score ties switch together. Recompute only scenes receiving new candidates.
        events = {}
        for i,row in enumerate(rows):
            for value in row['scores'][arm]:
                events.setdefault(value,set()).add(i)
        counts = [measure([r],'whole',0.) for r in rows]
        matched = predicted = 0
        best = None
        for threshold in sorted(events,reverse=True):
            for i in events[threshold]:
                update = measure([rows[i]],arm,threshold)
                matched += update['matched'] - counts[i]['matched']
                predicted += update['predicted'] - counts[i]['predicted']
                counts[i] = update
            if matched>=np.ceil(RETENTION*maximum) and predicted:
                choice = (matched/predicted,matched,-predicted,float(threshold))
                if best is None or choice>best:
                    best=choice
        choices[arm] = best[-1] if best else None
    return choices


def run(source, release, early, development, later, direct, anchor, output):
    output = output_path(output,DATASET)
    if output.exists() or digest(source)!=DATA_SHA256 or digest(release)!=LABEL_SHA256:
        raise ValueError('候选复核要求新目录和冻结标注')
    protocol = read(direct/'protocol.json')
    old = read(direct/'summary.json')
    weights = direct/'direct.pt'
    baseline = anchor/'mixed_gong/last.pt'
    if (old['test_accessed'] or old['model_sha256']!=digest(weights)
            or old['protocol_sha256']!=digest(direct/'protocol.json')
            or read(anchor/'summary.json')['results']['mixed_gong']['weights_sha256']!=digest(baseline)):
        raise ValueError('直接模型或基线身份不符')
    _, original = inventory(source,release)
    _, history = later_pairs(source,release)
    early_rows = read(early/'inventory.json')
    source_receipt = read(early/'summary.json')
    if source_receipt['inventory_sha256']!=digest(early/'inventory.json'):
        raise ValueError('训练数据来源收据不符')
    excluded = {datetime.strptime(name[:8],'%Y%m%d')+timedelta(days=delta)
                for name in protocol['samples']['fit'] for delta in (-1,0,1)}
    available = sorted(r['file'] for r in early_rows if r['file'][:4]<'2017'
                       and datetime.strptime(r['file'][:8],'%Y%m%d') not in excluded)
    names = [available[i] for i in np.linspace(0,len(available)-1,min(144,len(available))).round().astype(int)]
    if len(names)<60:
        raise ValueError('跨日期复核训练资料不足')
    cohorts = {'fit':(early,names,original),'development':(development,protocol['samples']['development'],original),
               '2019_2020':(later,protocol['samples']['2019_2020'],history)}
    atomic_json(output/'protocol.json',{'code_sha256':digest(__file__),'model_sha256':digest(weights),
        'policy':'2017 chooses each threshold, 90% of raw strict matches retained on development when filtering standalone',
        'additional_policy':'2017 requires >=3 added strict, added unmatched <= gained',
        'primary':PRIMARY,'samples':{k:rows for k,(_,rows,_) in cohorts.items()},
        'training_exclusion':'direct-detector fit date and adjacent UTC days excluded',
        'training_outcome':'>=0.5 in every annotation, proxy background with 0 overlap in every annotation',
        'test_accessed':False,'fresh_days_accessed':False})
    whole, net = AuthorGraph('batch'), architecture()
    whole.load_state_dict(load_checkpoint(baseline)['model'])
    state = torch.load(weights,map_location='cpu',weights_only=True)
    if state['protocol_sha256']!=old['protocol_sha256']:
        raise ValueError('直接模型训练协议不符')
    net.load_state_dict(state['model'])
    cv2.setNumThreads(2)
    torch.set_num_threads(2)
    data, results = {}, {}
    with exclusive(ROOT/'.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.backends.cuda.matmul.allow_tf32=False
        torch.backends.cudnn.allow_tf32=False
        whole.cuda().eval()
        net.cuda().eval()
        meter=Resources()
        for phase,(folder,list_names,table) in cohorts.items():
            lookup={r['file']:r for r in read(folder/'inventory.json')}
            entries=[]
            for i,name in enumerate(list_names,1):
                source_row=lookup[name]
                marks=[table[a['id']] for a in source_row['annotators']]
                cases=[r.values() if isinstance(r,dict) else r for r in marks]
                entries.append({'file':name,**material(image_bytes(folder,source_row),cases,whole,net)})
                if i%12==0:
                    atomic_json(output/'status.json',{'state':phase,'done':i,'total':len(list_names),'resources':meter.sample()})
            atomic_json(output/f'{phase}-geometry.json',entries)
            if phase=='fit':
                xy=[(x,y,1. if row['variants']>=2 else .6) for row in entries
                    for x,y in zip(row['features'],row['targets'],strict=True) if y>=0]
                xx,yy,w=map(np.asarray,zip(*xy))
                count=np.bincount(yy,minlength=2)
                if count.min()<30:
                    raise ValueError('严格匹配复核正负参考不足')
                model=HistGradientBoostingClassifier(max_iter=90,max_leaf_nodes=12,l2_regularization=2.,learning_rate=.065,
                                                    early_stopping=False,random_state=2026)
                model.fit(xx,yy,sample_weight=w*len(yy)/(2*count[yy]))
                atomic_json(output/'fit-metrics.json',{'strict_positive':int(count[1]),'proxy_background':int(count[0]),
                                                      'observations':len(entries)})
                continue
            rank(entries,model)
            data[phase]=entries
        resources=meter.sample()
    for phase,entries in data.items():
        standalone=[r['standalone'] for r in entries]
        added=[r['additional'] for r in entries]
        if phase=='development':
            points=select_retained(standalone)
            compatible=[{**r,'scores':{'visual':r['scores']['learned'],'features':r['scores']['centers']}} for r in added]
            additional=exact_policy(compatible)
            atomic_json(output/'policy.json',{'standalone':points,'baseline_plus':additional})
        unfiltered=measure(standalone,'all',0.)
        known=old['scores'][phase]['independent']['direct_050']
        if unfiltered['matched']!=known['matched_iou_050'] or unfiltered['predicted']!=known['predicted_regions_total_across_variants']:
            raise ValueError('原直接模型的严格对照未复现')
        results[phase]={'raw':unfiltered,'whole':measure(added,'whole',0.),'all_additions':measure(added,'all',0.),
            'retention':{arm:measure(standalone,arm,c) for arm,c in points.items() if c is not None},
            'baseline_plus':{key:measure(added,arm,additional[key]['cutoff']) for key,arm in
                             (('visual','learned'),('features','centers')) if additional[key]['cutoff'] is not None}}
        atomic_json(output/f'{phase}-cases.json',entries)
    result={'state':'completed','created_utc':now(),'scores':results,'policy':{'standalone':points,'baseline_plus':additional},
            'protocol_sha256':digest(output/'protocol.json'),'cases_sha256':{k:digest(output/f'{k}-cases.json') for k in results},
            'resources':resources,'test_accessed':False,'deployed':False}
    atomic_json(output/'summary.json',result)
    atomic_json(output/'status.json',{'state':'completed','summary_sha256':digest(output/'summary.json')})
    return result


if __name__=='__main__':
    configure_environment()
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('source','release','early','development','later','direct','anchor','output'):
        parser.add_argument('--'+key,type=Path,required=True)
    r=run(**vars(parser.parse_args()))
    print(json.dumps({'state':r['state'],'policy':r['policy']},ensure_ascii=False))
