"""Measure added forecasting value from signed field inside detected active regions."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image
from safetensors.torch import load_file
from scipy.ndimage import gaussian_filter, maximum_filter, minimum_filter
from sklearn.metrics import average_precision_score
import torch

from .ar_hmi_registration import DISPLAY_RADIUS
from .ar_nrt_feature_replay import prediction
from .ar_scale_study import ROOT, package_info
from .datasets import SegmentationDataset
from .durable import atomic_json
from .forecast_disk_baseline import RAW
from .forecast_disk_xrs import XRS, scores, train_fit
from .journal import read
from .metrics import forecast_scores, paired_cluster_interval
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path
from .xrs_coverage import utc


METRICS = ('mean_abs', 'p99_abs', 'strong_fraction', 'mixed_gradient')
NAMES = {region:tuple(f'{region}_{m}' for m in METRICS) for region in ('disk','active','rotated')}
ARMS = {
    'goes':XRS,
    'goes_display':(*XRS,*RAW),
    'goes_los_disk':(*XRS,*NAMES['disk']),
    'goes_los_active':(*XRS,*NAMES['disk'],*NAMES['active']),
    'goes_los_rotated':(*XRS,*NAMES['disk'],*NAMES['rotated']),
}
PAIRS = (
    ('goes','goes_los_disk'), ('goes_los_disk','goes_los_active'),
    ('goes_los_rotated','goes_los_active'), ('goes_display','goes_los_active'),
)


def gradients(field):
    """Approximate local gradients near mixed strong polarity on the display grid."""
    finite = np.isfinite(field)
    weights = gaussian_filter(finite.astype(np.float32),1.2)
    smooth = gaussian_filter(np.where(finite,field,0.).astype(np.float32),1.2) / np.maximum(weights,.01)
    safe = minimum_filter(finite,size=7) & (weights>=.98)
    mixed = safe & (maximum_filter(smooth,size=7)>=75.) & (minimum_filter(smooth,size=7)<=-75.)
    near_zero = np.abs(smooth)<=20.
    strength = np.hypot(*np.gradient(smooth))
    return np.where(mixed & near_zero,strength,0.)


def summarize(field, gradient, mask):
    values = np.abs(field[mask])
    if not len(values):
        return (0.,0.,0.,0.)
    return (float(np.log1p(values.mean())),float(np.log1p(np.quantile(values,.99))),
            float(np.mean(values>=100.)),float(np.log1p(gradient[mask].mean())))


def physical_features(field, detected):
    if field.shape!=(1024,1024) or detected.shape!=field.shape:
        raise ValueError('磁场与活动区掩膜网格不一致')
    y,x = np.ogrid[:1024,:1024]
    core = (y-511.5)**2+(x-511.5)**2 <= (.72*DISPLAY_RADIUS)**2
    finite = np.isfinite(field)
    usable = core & finite & finite[::-1,::-1]
    if usable.sum()<.98*core.sum():
        raise ValueError('中心日面共同磁场支撑不足')
    predicted = detected & usable
    rotated = predicted[::-1,::-1]
    strength = gradients(field)
    values = {key:value for name,mask in (
        ('disk',usable),('active',predicted),('rotated',rotated))
        for key,value in zip(NAMES[name],summarize(field,strength,mask),strict=True)}
    return values, {'disk':int(usable.sum()),'active':int(predicted.sum()),'rotated':int(rotated.sum()),
                    'active_overlap_with_rotation':int((predicted & rotated).sum())}


def ap_interval(y, first, second, dates, repeats=2000):
    groups = np.asarray(dates)
    blocks = [np.flatnonzero(groups==key) for key in np.unique(groups)]
    rng = np.random.default_rng(2026)
    changes = []
    for _ in range(repeats):
        idx = np.concatenate([blocks[n] for n in rng.integers(len(blocks),size=len(blocks))])
        labels = y[idx]
        if not labels.any() or labels.all():
            continue
        changes.append(average_precision_score(labels,second[idx])-average_precision_score(labels,first[idx]))
    return {'difference':float(average_precision_score(y,second)-average_precision_score(y,first)),
            'ci95':np.quantile(changes,[.025,.975]).tolist() if changes else None,
            'blocks':len(blocks),'valid_repeats':len(changes)}


def local_file(name, expected):
    approved = ROOT/'out/solar-observation-audit-20261118'
    path = (ROOT/name).resolve()
    if not path.is_relative_to(approved.resolve()) or digest(path)!=expected:
        raise ValueError('物理磁场档案身份不符')
    return path


def run(dataset, package, baseline, output):
    output = output_path(output,dataset)
    receipt_path = output/'input-receipt.json'
    receipt = read(receipt_path)
    if (not receipt or receipt['state']!='completed' or receipt['test_accessed'] or receipt['calibration_accessed']
            or (output/'summary.json').exists() or receipt['dataset_sha256']!=digest(dataset/'manifest-hashes.json')):
        raise ValueError('需要固定磁图回放清单')
    if any(digest(ROOT/name)!=expected for name,expected in receipt['inputs'].items()):
        raise ValueError('磁场或 GOES 上游清单已变化')
    card, constructor, weights = package_info(package)
    baseline_summary = read(baseline/'summary.json')
    if (baseline_summary['protocol_sha256']!=digest(baseline/'protocol.json')
            or read(baseline/'protocol.json')['model_sha256']!=card['weights_sha256']):
        raise ValueError('需要与历史图像基线一致的活动区模型')
    protocol = {'code_sha256':digest(__file__),'receipt_sha256':digest(receipt_path),
        'weights_sha256':digest(weights),'arms':{k:list(v) for k,v in ARMS.items()},'comparisons':[list(p) for p in PAIRS],
        'sampling':'same sample pool; chronological fit and later development',
        'target':'disk-wide M1+ within the 24 h after hypothetical issue at observation +70 min',
        'rotated_control':'180-degree rotation on common valid central disk, identical projected area',
        'proxies':'signed LOS Gauss, gradients in display-grid G/pixel; no radial WCS, R-value, or magnetic free energy',
        'detection_training':'AR segmentation predictions on the training years are not out-of-fold',
        'output_scope':'historical research ranking, not an operational calibrated probability',
        'test_accessed':False,'calibration_accessed':False}
    previous = read(output/'protocol.json')
    if previous is None:
        atomic_json(output/'protocol.json',protocol)
    elif previous!=protocol:
        raise ValueError('预报回放协议已改变')
    dataset_by_split = {s:SegmentationDataset(dataset,'AR',s,512) for s in ('train','validation')}
    model,_ = constructor(card['config'])
    model.load_state_dict(load_file(weights))
    torch.set_num_threads(2)
    rows = []
    with exclusive(ROOT/'.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.backends.cuda.matmul.allow_tf32=False
        torch.backends.cudnn.allow_tf32=False
        model.cuda().eval()
        resource = Resources()
        for item in receipt['samples']:
            local_file(item['fits'],item['fits_sha256'])
            array = local_file(item['array'],item['array_sha256'])
            filename = dataset_by_split[item['split']].source(item['uri'])
            if digest(filename)!=item['source_jpeg_sha256']:
                raise ValueError('活动区图像文件已变化')
            with Image.open(filename) as image:
                gray = np.asarray(image.convert('L'))
            mask = prediction(model,gray)
            field = np.load(array,allow_pickle=False)
            values, area = physical_features(field,mask)
            features = {**item['xrs'],**{k:item['legacy_features'][k] for k in RAW},**values}
            rows.append({**{key:item[key] for key in ('uri','split','observed_utc','issue_utc','target_end_utc','target')},
                         'features':features,'area_pixels':area})
            if len(rows)%32==0:
                atomic_json(output/'status.json',{'state':'extracting','done':len(rows),'total':len(receipt['samples']),
                                                   'resources':resource.sample()})
        metrics = resource.sample()
    training = [r for r in rows if r['split']=='train']
    later = [r for r in rows if r['split']=='validation']
    if max(utc(r['target_end_utc']) for r in training)>=min(utc(r['issue_utc']) for r in later):
        raise ValueError('跨时间划分存在标签重叠')
    truth = np.asarray([r['target'] for r in later])
    climo = sum(r['target'] for r in training)/len(training)
    groups = [utc(r['issue_utc']).date().toordinal()//27 for r in later]
    predicted = {name:scores(train_fit(training,keys),later,keys) for name,keys in ARMS.items()}
    result = {name:forecast_scores(truth,v,climate=climo) for name,v in predicted.items()}
    comparisons = {f'{b}_vs_{a}':{
        'ap':ap_interval(truth,predicted[a],predicted[b],groups),
        'brier':paired_cluster_interval((predicted[a]-truth)**2,(predicted[b]-truth)**2,groups),
    } for a,b in PAIRS}
    atomic_json(output/'features.json',rows)
    atomic_json(output/'development-scores.json',[{'uri':row['uri'],'target':row['target'],
        'scores':{arm:float(p[i]) for arm,p in predicted.items()}} for i,row in enumerate(later)])
    atomic_json(output/'summary.json',{'state':'completed','recorded_utc':now(),
        'protocol_sha256':digest(output/'protocol.json'),'features_sha256':digest(output/'features.json'),
        'cohort':{f'{lane}_{target}':n for (lane,target),n in Counter((r['split'],r['target']) for r in rows).items()},
        'development_metrics':result,'paired_comparisons':comparisons,'resources':metrics,
        'empty_activity_masks':sum(not r['area_pixels']['active'] for r in rows),
        'test_accessed':False,'calibration_accessed':False,'issued_forecast':False})
    atomic_json(output/'status.json',{'state':'completed','summary_sha256':digest(output/'summary.json')})


if __name__=='__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset','package','baseline','output'):
        parser.add_argument('--'+name,required=True,type=Path)
    run(**vars(parser.parse_args()))
