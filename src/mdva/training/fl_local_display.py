"""Fit and replay local H-alpha display conditioning without changing image coordinates."""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from scipy.ndimage import label
import torch

from .durable import atomic_json
from .fl_direct_centers import infer, input_planes
from .fl_direct_reviewer import evidence
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT
from .fl_magfilo_audit import DATA_SHA256, raster
from .fl_shape_fresh import verified_checkpoint
from .fl_shape_statistics import paired
from .fl_station_normalization import RADII, corrected, disk_profile
from .fl_swefil_paired_domain import score_instances, variant_pairs
from .fl_swefil_review import RAW, TRAIN_SHA, fetch
from .fl_visual_candidate_review import measure
from .journal import read
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


QUANTILES = np.linspace(.01,.99,99)
MODES = ('raw','radial','tone','radial_tone')


def radial_map(shape, radius):
    y, x = np.ogrid[:shape[0], :shape[1]]
    return np.hypot(y-(shape[0]-1)/2,x-(shape[1]-1)/2) / radius


def tone_map(gray, region, target):
    levels = np.quantile(gray[region],QUANTILES)
    values, indices = np.unique(levels,return_index=True)
    if len(values)<5:
        raise ValueError('Hα日面内有效色阶不足')
    mapped = np.interp(gray,values,np.asarray(target)[indices])
    return mapped.astype(np.float32)


def planes_from_gray(gray, valid):
    support = valid.astype(np.float32)
    fields = [gray*2-1]
    for sigma in (2,9):
        mean = cv2.GaussianBlur(gray * support,(0,0),sigma) / np.maximum(
            cv2.GaussianBlur(support,(0,0),sigma),.02)
        fields.append(np.clip((mean-gray)*12,-2,2))
    return np.where(valid[None],np.stack(fields),0).astype(np.float32)


def display_variants(inputs, support, radius, target):
    gray = (inputs[0]+1)*.5
    rho = radial_map(gray.shape,2*radius)
    region = support & (rho<=.945)
    profile = disk_profile(gray,rho)
    flat = {'radial':[profile['median']]*len(RADII),'median':profile['median']}
    radial = corrected(gray,rho,profile,flat)
    return {
        'raw':inputs,
        'radial':planes_from_gray(radial,support),
        'tone':planes_from_gray(tone_map(gray,region,target),support),
        'radial_tone':planes_from_gray(tone_map(radial,region,target),support),
    }


def fit_inventory(source):
    train, tree = read(source/'train.json'),read(source/'image-tree.json')
    names = {r['file_name'] for r in train['images']}
    links = {r['path']:r['lfs']['oid'] for r in tree if r.get('lfs')}
    groups = defaultdict(list)
    for file in sorted(names):
        raw = RAW.fullmatch(file)
        if raw and '2010'<=raw['stamp'][:4]<='2015':
            processed = file.removesuffix('_raw.jpg')+'.jpg'
            if processed in names and processed in links:
                groups[raw['site']].append({'file':processed,'sha256':links[processed]})
    if len(groups)!=6 or min(map(len,groups.values()))<4:
        raise ValueError('早期灰度参考没有六台站')
    return [items[i] for _,items in sorted(groups.items())
            for i in np.linspace(0,len(items)-1,4).round().astype(int)]


def fetch_target(output, records):
    with ThreadPoolExecutor(max_workers=6) as pool:
        receipts = list(pool.map(lambda x:fetch(output,x),records))
    atomic_json(output/'target-download.json',receipts)
    if any('error' in row for row in receipts):
        raise ValueError('早期图像下载校验未完成，可原目录续跑')
    templates = []
    for row in receipts:
        path = output/row['file']
        if digest(path)!=row['sha256']:
            raise ValueError('灰度参考身份不符')
        values, support, radius, _ = input_planes(path.read_bytes())
        inside = support & (radial_map(support.shape,2*radius)<=.945)
        templates.append(np.quantile((values[0,inside]+1)*.5,QUANTILES))
    target = np.median(templates,axis=0).tolist()
    atomic_json(output/'reference-tone.json',{'quantiles':QUANTILES.tolist(),'target':target,
        'image_receipt_sha256':digest(output/'target-download.json')})
    return target


def run(source, magfilo, study, output):
    output = output_path(output, DATASET)
    source = source.resolve()
    file = magfilo/'magfilo_2024_v1.0.json'
    if (output/'summary.json').exists() or digest(source/'train.json')!=TRAIN_SHA or digest(file)!=DATA_SHA256:
        raise ValueError('需要冻结的公开标注和独立研究目录')
    prior = study/'fl-swefil-review-01'
    pair = study/'fl-swefil-paired-domain-01'
    compare = study/'fl-shape-replay-01'
    p, paired_info = read(prior/'protocol.json'),read(pair/'summary.json')
    if any(r['test_accessed'] for r in (p,paired_info,read(compare/'summary.json'))):
        raise ValueError('仅可读取历史研究记录')
    if (digest(source/'image-tree.json') != p['source_image_tree_sha256']
            or digest(pair/'protocol.json') != paired_info['protocol_sha256']):
        raise ValueError('已核验的文件配对身份改变')
    raw = variant_pairs(read(source/'train.json'),p['samples'],read(source/'image-tree.json'))
    evaluate = [{'path':prior/r['raw']['file'],'id':r['raw']['file'],'site':r['raw']['site'],
                 'sha256':r['raw']['sha256'],'variants':[r['filament_labels']]} for r in raw]
    old = study/'fl-gong-fused-2019-2020-01'
    refs = defaultdict(list)
    for ann in read(file)['annotations']:
        refs[ann['image_id']].append(ann)
    cohort = {r['file']:r for r in read(old/'inventory.json')}
    late = []
    for name in read(compare/'protocol.json')['samples']['2019_2020']:
        row = cohort[name]
        late.append({'path':old/'jpeg'/name,'id':name,'site':name[14:16],'sha256':row['image_sha256'],
                     'variants':[refs[i['id']] for i in row['annotators']]})
    training = fit_inventory(source)
    protocol = {'code_sha256':digest(__file__),'pair_summary_sha256':digest(pair/'summary.json'),
        'swefil_train_sha256':TRAIN_SHA,'fit_records':training,'variants':list(MODES),
        'fit_method':'median of processed 1%-99% quantiles, four 2010–2015 images per site, label-free',
        'radial_method':'existing smooth ring-median correction, flatten to within-image central median',
        'holdout_swefil':[r['id'] for r in evaluate],'transfer_magfilo':[r['id'] for r in late],
        'selection_uses_holdout':False,'test_accessed':False,'threshold':.5}
    previous = read(output/'protocol.json')
    if previous is None:
        atomic_json(output/'protocol.json',protocol)
    elif previous!=protocol:
        raise ValueError('日面试验协议已改变')
    cv2.setNumThreads(2)
    torch.set_num_threads(2)
    tone = fetch_target(output,training)
    candidate = study/'fl-shape-objective-01'
    model_info = read(candidate/'summary.json')
    model = verified_checkpoint(candidate/'edge_centerline/last.pt',p['weights']['edge_centerline'],
                               [model_info['protocol_sha256'],'edge_centerline'])
    final, intervals = {}, {}
    with exclusive(ROOT/'.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.backends.cuda.matmul.allow_tf32=False
        torch.backends.cudnn.allow_tf32=False
        model.cuda().eval()
        resources = Resources()
        for name, observations in (('swefil',evaluate),('magfilo',late)):
            cases = []
            for row in observations:
                if digest(row['path'])!=row['sha256']:
                    raise ValueError('评估输入指纹不符')
                inputs, valid, radius, _ = input_planes(row['path'].read_bytes())
                planes = display_variants(inputs,valid,radius,tone)
                references = [label(raster(v),structure=CONNECTIVITY)[0] for v in row['variants']]
                masks = [raster([r]) for r in row['variants'][0]] if name=='swefil' else []
                arms = {}
                for mode, patch in planes.items():
                    pred = infer(model,patch,valid)[0]>=.5
                    count = label(pred,structure=CONNECTIVITY)[1]
                    arms[mode+'_region'] = {'count':count,'base_count':0,'geometry':evidence(pred,references)}
                    if name=='swefil':
                        arms[mode+'_instance'] = score_instances(pred,masks,[r['id'] for r in row['variants'][0]])
                    resources.sample()
                cases.append({'file':Path(row['id']).name,'site':row['site'],'arms':arms})
            keys = tuple(cases[0]['arms'])
            final[name] = {k:measure([r['arms'][k] for r in cases],'all',0.) for k in keys}
            checks = paired_info['overall'] if name=='swefil' else read(study/'fl-shape-objective-01/summary.json')['results']['2019_2020']
            if name=='swefil' and any(final[name][f'raw_{mode}']!=checks[f'raw_edge_centerline_{mode}'] for mode in ('region','instance')):
                raise ValueError('SWEFil原图对照未复现')
            if name=='magfilo' and final[name]['raw_region']!=checks['edge_centerline']:
                raise ValueError('MAGFiLO原图对照未复现')
            intervals[name] = {k:paired(cases,k,'raw_'+k.rsplit('_',1)[1])
                               for k in keys if not k.startswith('raw_')}
            atomic_json(output/f'{name}-cases.json',cases)
        observed = resources.sample()
    atomic_json(output/'summary.json',{'state':'completed','recorded_utc':now(),'results':final,'paired':intervals,
        'protocol_sha256':digest(output/'protocol.json'),'tone_sha256':digest(output/'reference-tone.json'),
        'resources':observed,'test_accessed':False,'deployed':False,'author_test_accessed':False})
    atomic_json(output/'status.json',{'state':'completed','summary_sha256':digest(output/'summary.json')})


if __name__=='__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source','magfilo','study','output'):
        parser.add_argument('--'+name,required=True,type=Path)
    run(**vars(parser.parse_args()))
