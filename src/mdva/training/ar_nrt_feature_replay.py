"""Replay matched HMI versions through the frozen AR and flare research inputs."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import json

import numpy as np
from PIL import Image
from safetensors.torch import load_file
from scipy.ndimage import label
from scipy.special import expit
import torch
import torch.nn.functional as F

from .ar_hmi_registration import DISPLAY_RADIUS, FILENAME, SIZE, fits_field, sample, source_grid
from .ar_hmi_temporal_ranker import object_scores
from .ar_scale_study import ROOT, package_info
from .datasets import SegmentationDataset, read_frozen
from .durable import atomic_json, commit
from .forecast_disk_baseline import CORE, RAW, REGIONS, features
from .forecast_disk_xrs import ARMS
from .hmi_angular_projection import angular_sample
from .hmi_nrt_pair_audit import path_identity
from .journal import read
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


ORIENTATION = (1, -1)
Y, X = np.indices((SIZE, SIZE), dtype=float)
ARM = 'xrs_plus_raw_plus_regions'


def display(field, header):
    """Fixed historical-style rendering from Gauss; preserve NaNs until display."""
    physical = sample(field, source_grid(header), ORIENTATION, (0.,0.,1.), X,Y)
    disk = np.hypot(X-(SIZE-1)/2,Y-(SIZE-1)/2) < DISPLAY_RADIUS
    valid = np.isfinite(physical) & disk
    gray = np.full(physical.shape, 255, dtype=np.uint8)
    gray[valid] = np.clip(np.floor((np.clip(physical[valid],-500.,500.)+500.)*.256),0,255).astype(np.uint8)
    return gray


def prediction(model, gray):
    photo = Image.fromarray(gray)
    x = np.asarray(photo.resize((512,512), Image.Resampling.BILINEAR), dtype=np.float32)/255.
    tensor = torch.from_numpy(x)[None,None].cuda()
    with torch.autocast('cuda',dtype=torch.float16):
        output = model(tensor)
    if not torch.isfinite(output).all():
        output = model(tensor)
    if not torch.isfinite(output).all():
        raise ValueError('活动区推理输出非有限值')
    return F.interpolate(output.float().sigmoid(),size=gray.shape,mode='bilinear',align_corners=False)[0,0].cpu().numpy() >= .5


def mask_agreement(first, second):
    a,p = label(first)
    b,r = label(second)
    sizes = [{'area':int(v)} for v in np.bincount(a.ravel(),minlength=p+1)[1:]]
    result = object_scores(a,sizes,b,[True]*p)[0]
    n = int(first.sum()+second.sum())
    return {'pair_dice':2*int((first & second).sum())/n if n else None,'definitive_regions':p,'nrt_regions':r,
            'paired_iou_ge_50':result['matched']}


def feature_drift(before, after, weight):
    cols = weight['features']
    if cols!=list(ARMS[ARM]):
        raise ValueError('预测输入字段顺序变化')
    scales = np.asarray(weight['scale'],dtype=float)
    rates = np.asarray(weight['coef'],dtype=float)
    if not np.isfinite(scales).all() or min(scales)<=0 or not np.isfinite(rates).all():
        raise ValueError('预测模型系数或尺度异常')
    changes = {name:float(after[name]-before[name]) for name in (*RAW,*REGIONS)}
    impacts = {name:changes[name]*float(rates[cols.index(name)]/scales[cols.index(name)]) for name in changes}
    total = sum(impacts.values())
    # XRS and the operating point are held fixed, so their offsets cancel in logit space.
    # This is a worst-case sigmoid difference over a shared (unknown) baseline logit.
    bound = float(expit(abs(total)/2)-expit(-abs(total)/2))
    return {'magnetic_feature_delta':changes,'regional_logit_change':sum(impacts[k] for k in REGIONS),
            'total_logit_change_with_xrs_fixed':total,'max_possible_probability_change':bound}


def paired(capture, parity):
    rec = read(capture/'receipt.json')
    check = read(parity/'summary.json')
    if check['receipt_sha256']!=digest(capture/'receipt.json') or check['state']!='completed':
        raise ValueError('物理观测配对报告不匹配')
    known = {(r['nominal_tai'],r['flavor']):r for r in rec['rows']}
    for item in check['reports']:
        nominal = item['nominal_tai']
        if item['state']!='compared':
            continue
        result = {}
        for flavor in ('definitive','nrt'):
            row = known[nominal,flavor]
            name, url = path_identity(flavor,nominal)
            if (row['file']!=name or row['url']!=url or row['status']!='captured' or digest(capture/name)!=row['sha256']):
                raise ValueError('支持磁图的原文件身份改变')
            result[flavor] = capture/name
        yield nominal, item['quality'], result


def verify_render(dataset, registration, model, forecast_weight):
    declaration = read(registration/'summary.json')
    if declaration['protocol_sha256']!=digest(registration/'protocol.json') or declaration['test_accessed']:
        raise ValueError('训练图显示输入的对照身份不符')
    training = SegmentationDataset(dataset,'AR','train')
    files = {r['uri'] for r in training.rows}
    values = []
    for row in declaration['results']:
        if row['uri'] not in files or not row.get('registration',{}).get('accepted'):
            continue
        if tuple(row['registration']['orientation'])!=ORIENTATION:
            continue
        raw_name, _ = path_identity('definitive',row['source']['nominal_tai'])
        field_file = registration/'pairs'/row['id']/raw_name
        if digest(field_file)!=row['source']['sha256']:
            raise ValueError('既往定稿训练图原始指纹不同')
        with Image.open(training.source(row['uri'])) as image:
            jpeg = np.asarray(image.convert('L'))
        reconstructed = display(*fits_field(field_file))
        diff = jpeg[CORE].astype(float)-reconstructed[CORE]
        original_mask = prediction(model,jpeg)
        reconstructed_mask = prediction(model,reconstructed)
        regions = mask_agreement(original_mask,reconstructed_mask)
        drift = feature_drift(features(jpeg,original_mask),features(reconstructed,reconstructed_mask),forecast_weight)
        values.append({'uri':row['uri'],'gray_mae':float(abs(diff).mean()),'gray_correlation':float(np.corrcoef(jpeg[CORE],reconstructed[CORE])[0,1]),
                       'mask_pair_dice':regions['pair_dice'],'drift':drift,'regions':regions})
    return values


def run(dataset,captures,parity,registration,package,forecast,baseline,output):
    output=output_path(output,dataset)
    if output.exists() or len(captures)!=len(parity):
        raise ValueError('活动区版本对照须使用新的结果目录和配对资料')
    report=read(forecast/'summary.json')
    weight_file=forecast/'weights.json'
    if report['state']!='completed' or report['weights_sha256']!=digest(weight_file) or report['test_accessed']:
        raise ValueError('耀斑历史模型系数身份不符')
    weight=read(weight_file)[ARM]
    card,ctor,path=package_info(package)
    origin=read(baseline/'summary.json')
    if (card['id']!='AR-A0-s42' or card['category']!='AR'
            or report['baseline_summary_sha256']!=digest(baseline/'summary.json')
            or origin['protocol_sha256']!=digest(baseline/'protocol.json')
            or read(baseline/'protocol.json')['model_sha256']!=card['weights_sha256']):
        raise ValueError('须使用固定活动区图像检测器')
    locked = {m[1] for row in read_frozen(dataset,'segmentation-ready.jsonl')
              if row['category']=='AR' and row['split'] in ('test','calibration')
              and (m:=FILENAME.fullmatch(Path(row['uri']).name))}
    comparable = [r for capture, audit in zip(captures,parity,strict=True) for r in paired(capture,audit)]
    excluded = [n for n,_,_ in comparable if n in locked]
    atomic_json(output/'protocol.json',{'code_sha256':digest(__file__),'package_sha256':digest(path),
                'forecast_weights_sha256':digest(weight_file),'render_orientation':ORIENTATION,'render_clip_gauss':[-500,500],
                'test_accessed':False,'calibration_accessed':False,'excluded_protected_nominal_times':excluded,
                'forecast_policy':'magnetic feature-only logit response, hold XRS fixed; no forecast probabilities issued'})
    scores=[]
    with exclusive(ROOT/'.mdva/training-gpu.lock'),torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.backends.cudnn.allow_tf32=False
        torch.backends.cuda.matmul.allow_tf32=False
        resources=Resources()
        model,_=ctor(card['config'])
        model.load_state_dict(load_file(path))
        model.cuda().eval()
        verification=verify_render(dataset,registration,model,weight)
        for nominal,quality,files in comparable:
            if nominal in locked:
                continue
            a,ah=fits_field(files['definitive'])
            b,bh=fits_field(files['nrt'])
            aligned,_=angular_sample(b,bh,ah,a.shape)
            q=display(a,ah)
            w=display(aligned,ah)
            qa,wa=prediction(model,q),prediction(model,w)
            base, recent=features(q,qa),features(w,wa)
            masks=output/'pairs'/f'{nominal}.npz'
            commit(masks,lambda f:np.savez_compressed(f,definitive=qa,nrt=wa))
            scores.append({'nominal_tai':nominal,'quality':quality,'regions':mask_agreement(qa,wa),
                           'drift':feature_drift(base,recent,weight),'features':{'definitive':base,'nrt':recent},
                           'mask_sha256':digest(masks)})
            resources.sample()
        used=resources.sample()
    if not scores or not verification:
        raise ValueError('没有同时满足来源和显示检查的对照资料')
    atomic_json(output/'cases.json',scores)
    atomic_json(output/'historical-render.json',verification)
    groups={}
    for name,items in (('all',scores),('1024_to_0',[r for r in scores if r['quality']=={'nrt':1024,'definitive':0}])):
        if items:
            d=[r['regions']['pair_dice'] for r in items if r['regions']['pair_dice'] is not None]
            gaps=[abs(r['drift']['total_logit_change_with_xrs_fixed']) for r in items]
            groups[name]={'pairs':len(items),'pair_dice_min_median':[float(min(d)),float(np.median(d))] if d else None,
                          'changed_region_counts':sum(r['regions']['definitive_regions']!=r['regions']['nrt_regions'] for r in items),
                          'logit_absolute_delta_median_max':[float(np.median(gaps)),max(gaps)],
                          'max_probability_sensitivity_bound':max(r['drift']['max_possible_probability_change'] for r in items)}
    result={'state':'completed','recorded_utc':now(),'groups':groups,'excluded_protected_nominal_times':excluded,
            'historical_render':{'images':len(verification),'median_gray_mae':float(np.median([r['gray_mae'] for r in verification])),
                                 'median_gray_correlation':float(np.median([r['gray_correlation'] for r in verification])),
                                 'median_detector_mask_agreement':float(np.median([r['mask_pair_dice'] for r in verification if r['mask_pair_dice'] is not None])),
                                 'changed_region_counts':sum(r['regions']['definitive_regions']!=r['regions']['nrt_regions'] for r in verification),
                                 'median_max_score_response_bound':float(np.median([r['drift']['max_possible_probability_change'] for r in verification])),
                                 'largest_max_score_response_bound':max(r['drift']['max_possible_probability_change'] for r in verification)},
            'protocol_sha256':digest(output/'protocol.json'),'cases_sha256':digest(output/'cases.json'),
            'historical_render_sha256':digest(output/'historical-render.json'),'resources':used,
            'forecast_issued':False,'quality_gate_relaxed':False,'test_accessed':False,'calibration_accessed':False}
    atomic_json(output/'summary.json',result)
    return result


if __name__=='__main__':
    configure_environment()
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('dataset','registration','package','forecast','baseline','output'):
        p.add_argument('--'+name,type=Path,required=True)
    for name in ('captures','parity'):
        p.add_argument('--'+name,type=Path,nargs='+',required=True)
    print(json.dumps(run(**vars(p.parse_args())),ensure_ascii=False))
