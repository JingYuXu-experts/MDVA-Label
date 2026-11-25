"""Train H-alpha filament positions and masks without a proposal model."""

from __future__ import annotations

import argparse
from io import BytesIO
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from scipy.ndimage import binary_dilation, distance_transform_edt, find_objects, label
import torch
from torch import nn
from torch.nn import functional as F

from .ar_hmi_temporal_ranker import object_scores
from .durable import atomic_json, commit
from .fl_author_model import AuthorGraph
from .fl_contour_refiner import aggregate, score
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT
from .fl_gong_fused_verify import later_pairs
from .fl_gong_limb_gate import visible_disk
from .fl_gong_patch_training import image_bytes
from .fl_gong_resolution import predict
from .fl_magfilo_audit import DATA_SHA256, raster
from .fl_magfilo_transfer import LABEL_SHA256, inventory, load_gray
from .fl_native_detail_audit import native_raster
from .fl_visual_candidate_review import exact_policy, measure
from .journal import read
from .models import UNet
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


SIDE, TILE, STRIDE, BATCH, EPOCHS, SEED = 1024, 192, 144, 12, 24, 2026


def input_planes(raw):
    gray = load_gray(raw)
    disk, radius = visible_disk(gray)
    if disk is None:
        raise ValueError('直接检测要求已核验 Hα 日面范围')
    with Image.open(BytesIO(raw)) as source:
        native = np.asarray(source.convert('L'), dtype=np.float32) / 255
    img = cv2.resize(native, (SIDE, SIDE), interpolation=cv2.INTER_AREA)
    valid = np.repeat(np.repeat(disk, 2, axis=0), 2, axis=1)
    weight = valid.astype(np.float32)
    fields = [img * 2 - 1]
    for sigma in (2, 9):
        mean = cv2.GaussianBlur(img * weight, (0, 0), sigma) / np.maximum(cv2.GaussianBlur(weight, (0, 0), sigma), .02)
        fields.append(np.clip((mean - img) * 12, -2, 2))
    return np.where(valid[None], np.stack(fields), 0).astype(np.float32), valid, radius, gray


def tile_grid():
    edge = sorted(set(range(0, SIDE-TILE+1, STRIDE)) | {SIDE-TILE})
    return [(slice(y, y+TILE), slice(x, x+TILE)) for y in edge for x in edge]


def mark_centers(mask):
    y, x = np.mgrid[-10:11, -10:11]
    radial = np.exp(-(y*y + x*x) / 18).astype(np.float32)
    heat = np.zeros(mask.shape, np.float32)
    groups, _ = label(mask, structure=CONNECTIVITY)
    for i, (rows, cols) in enumerate(find_objects(groups), 1):
        area = groups[rows, cols] == i
        if area.sum() < 8:
            continue
        depth = distance_transform_edt(np.pad(area, 1))[1:-1, 1:-1]
        py, px = np.unravel_index(depth.argmax(), depth.shape)
        py, px = py + rows.start, px + cols.start
        a, b, c, d = max(0,py-10), min(heat.shape[0],py+11), max(0,px-10), min(heat.shape[1],px+11)
        target = heat[a:b,c:d]
        np.maximum(target, radial[a-py+10:b-py+10,c-px+10:d-px+10], out=target)
    return heat


def fitting_tiles(fields, valid, references, rng):
    objects = []
    for item in references:
        original = native_raster(item)
        native = original.reshape(SIDE, 2, SIDE, 2).any(axis=(1,3))
        objects.append(native)
    contour = np.mean(objects, axis=0, dtype=np.float32)
    center = np.maximum.reduce([mark_centers(r) for r in objects])
    chosen, empty = [], []
    for window in tile_grid():
        if valid[window].mean() < .40:
            continue
        peak = int((center[window] == 1).sum())
        if peak:
            chosen.append((window, peak))
        elif not contour[window].any():
            empty.append((window, float(np.quantile(fields[2][window], .99))))
    # Broad supervision across observations; include a dark unlabeled tile and a random other tile.
    shortlist = rng.permutation(len(chosen))[:4]
    windows = [chosen[i][0] for i in shortlist]
    empty.sort(key=lambda row: -row[1])
    if empty:
        windows.append(empty[0][0])
        if len(empty) > 1:
            windows.append(empty[int(rng.integers(1, len(empty)))][0])
    targets = np.stack((contour, center))
    return [(fields[(slice(None),*w)].astype(np.float16), targets[(slice(None),*w)].astype(np.float16),
             valid[w], int((center[w] == 1).sum())) for w in windows]


def architecture():
    net = UNet(width=8)
    net.encoders[0].layers[0] = nn.Conv2d(3, 8, 3, padding=1)
    for block in (*net.encoders, *net.decoders):
        initial, _, second, _ = block.layers
        block.layers = nn.Sequential(initial, nn.GroupNorm(4, initial.out_channels), nn.SiLU(),
                                     second, nn.GroupNorm(4, second.out_channels), nn.SiLU())
    net.out = nn.Conv2d(8, 2, 1)
    with torch.no_grad():
        net.out.bias.copy_(torch.tensor([-2., -4.]))
    return net


def focal_center(logits, heat, support):
    p = logits.sigmoid()
    targets = (heat == 1) * support
    other = (heat < 1) * support
    positive = -(F.logsigmoid(logits) * (1-p).square() * targets).sum()
    negative = -(F.logsigmoid(-logits) * p.square() * (1-heat).pow(4) * other).sum()
    return (positive + negative) / targets.sum().clamp_min(1)


def objective(logits, targets, valid):
    mask, center = logits[:,0], logits[:,1]
    label, heat = targets[:,0], targets[:,1]
    pos, neg = label * valid, (1-label) * valid
    p_loss = (-F.logsigmoid(mask) * pos).sum() / pos.sum().clamp_min(1)
    n_loss = (-F.logsigmoid(-mask) * neg).sum() / neg.sum().clamp_min(1)
    estimates = mask.sigmoid() * valid
    dice = 1 - ((2*(estimates*pos).sum((1,2))+1) / ((estimates+pos).sum((1,2))+1)).mean()
    return .25*p_loss + .40*n_loss + .35*dice + .15*focal_center(center, heat, valid)


def train(data, output, protocol, usage):
    x, y, available, counts = zip(*data)
    x, y, available = map(np.stack,(x,y,available))
    atomic_json(output/'fitting.json', {'tiles':len(x), 'tiles_with_centers':sum(bool(c) for c in counts),
                                     'centers_across_tiles':sum(counts)})
    torch.manual_seed(SEED)
    net = architecture().cuda()
    optimizer = torch.optim.AdamW(net.parameters(), lr=.001)
    for epoch in range(EPOCHS):
        order = np.random.default_rng(SEED+epoch).permutation(len(x))
        losses = []
        for start in range(0,len(x),BATCH):
            idx = order[start:start+BATCH]
            imgs, target, valid = [torch.as_tensor(a[idx],dtype=torch.float32,device='cuda') for a in (x,y,available)]
            optimizer.zero_grad(set_to_none=True)
            loss = objective(net(imgs),target,valid)
            if not torch.isfinite(loss):
                raise ValueError('直接检测模型训练数值异常')
            loss.backward()
            torch.nn.utils.clip_grad_norm_(net.parameters(),4,error_if_nonfinite=True)
            optimizer.step()
            losses.append(float(loss.detach()))
        checkpoint = {'model':net.state_dict(),'optimizer':optimizer.state_dict(),'epoch':epoch+1,
                      'protocol_sha256':protocol}
        commit(output/'direct.pt',lambda f:torch.save(checkpoint,f))
        atomic_json(output/'status.json', {'state':'training_direct','epoch':epoch+1,'total':EPOCHS,
                                        'loss':float(np.mean(losses)),'resources':usage.sample()})
        if (output/'PAUSE').exists():
            return None
    return net.eval()


def infer(net, fields, valid):
    weight = np.maximum(np.outer(np.hanning(TILE),np.hanning(TILE)),.08).astype(np.float32)
    maps = np.zeros((2,SIDE,SIDE),dtype=np.float32)
    support = np.zeros((SIDE,SIDE),dtype=np.float32)
    locations = [w for w in tile_grid() if valid[w].any()]
    for start in range(0,len(locations),BATCH):
        batch = locations[start:start+BATCH]
        x = torch.as_tensor(np.stack([fields[(slice(None),*w)] for w in batch]),device='cuda')
        pred = net(x).sigmoid().cpu().numpy()
        if not np.isfinite(pred).all():
            raise ValueError('直接检测输出非有限值')
        for w,a in zip(batch,pred,strict=True):
            maps[(slice(None),*w)] += a*weight
            support[w] += weight
    if (support[valid] <= 0).any():
        raise ValueError('滑窗未覆盖经核验的日面')
    maps = maps / np.maximum(support, .001)
    maps *= valid
    return [cv2.resize(a,(512,512),interpolation=cv2.INTER_AREA) for a in maps]


def additions(baseline, direct, contour, centers, references):
    prior, n = label(baseline, structure=CONNECTIVITY)
    protected = binary_dilation(baseline, structure=CONNECTIVITY, iterations=2)
    regions, _ = label(direct, structure=CONNECTIVITY)
    removed = np.unique(regions[protected])
    safe = (regions != 0) & ~np.isin(regions,removed)
    extra, m = label(safe, structure=CONNECTIVITY)
    combo = np.where(safe, n + extra, prior)
    parts = [{'area':int(v)} for v in np.bincount(combo.ravel())[1:]]
    shape, spots = [], []
    for i,box in enumerate(find_objects(extra),1):
        part = extra[box]==i
        shape.append(float(contour[box][part].mean()))
        spots.append(float(centers[box][part].max()))
    comparison = [object_scores(combo,parts,label(r,structure=CONNECTIVITY)[0],[True]*(n+m))[1] for r in references]
    return {'base_count':n, 'count':m,'scores':{'visual':spots,'features':shape},'geometry':comparison}


def run(source, release, early, development, later, prior, base, output):
    output = output_path(output, DATASET)
    if output.exists() or digest(source)!=DATA_SHA256 or digest(release)!=LABEL_SHA256:
        raise ValueError('直接检测需冻结标注和独立结果目录')
    previous = read(prior/'summary.json')
    scheme = read(prior/'protocol.json')
    weights = base/'mixed_gong/last.pt'
    if (previous['test_accessed'] or '2019_2020' not in scheme['samples'] or
            digest(prior/'protocol.json')!=previous['protocol_sha256'] or digest(weights)!=scheme['weight_sha256'][0]):
        raise ValueError('早期日期划分或对照基线身份不符')
    _, ref = inventory(source,release)
    _, other = later_pairs(source,release)
    phases = {'fit':(early,ref),'development':(development,ref),'2019_2020':(later,other)}
    atomic_json(output/'protocol.json', {'created_utc':now(),'code_sha256':digest(__file__),'previous_sha256':digest(prior/'summary.json'),
        'samples':scheme['samples'],'grid':SIDE,'tile':TILE,'stride':STRIDE,'epochs':EPOCHS,
        'mask_threshold':.5,'design':'annotated interior centers with CenterNet focal term, independent H-alpha contour head',
        'method_source':'https://arxiv.org/abs/1904.07850','test_accessed':False,'fresh_years_accessed':False})
    cv2.setNumThreads(2)
    torch.set_num_threads(2)
    comparator = AuthorGraph('batch')
    comparator.load_state_dict(load_checkpoint(weights)['model'])
    results={}
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.backends.cuda.matmul.allow_tf32=False
        torch.backends.cudnn.allow_tf32=False
        comparator.cuda().eval()
        usage=Resources()
        for phase,(folder,mapping) in phases.items():
            lookup={r['file']:r for r in read(folder/'inventory.json')}
            names=scheme['samples'][phase]
            entries=[]
            for i,name in enumerate(names,1):
                row=lookup[name]
                raw=image_bytes(folder,row)
                fields,valid,radius,gray=input_planes(raw)
                annotations=[mapping[v['id']] for v in row['annotators']]
                annotations=[list(v.values()) if isinstance(v,dict) else v for v in annotations]
                if phase=='fit':
                    entries.extend(fitting_tiles(fields,valid,annotations,np.random.default_rng(SEED+i)))
                else:
                    with torch.inference_mode():
                        old=predict(comparator,gray[None])[0]>=.5
                        mask,centers=infer(model,fields,valid)
                    references=[raster(v) for v in annotations]
                    direct=mask>=.5
                    arms={'whole':old,'direct_035':mask>=.35,'direct_050':direct,'direct_065':mask>=.65}
                    evidence=additions(old,direct,mask,centers,references)
                    entries.append({'file':name,'arms':score(arms,references,radius),'additional':evidence})
                if i%12==0:
                    atomic_json(output/'status.json', {'state':phase,'done':i,'total':len(names),'resources':usage.sample()})
            if phase=='fit':
                model=train(entries,output,digest(output/'protocol.json'),usage)
                if model is None:
                    return {'state':'paused'}
                continue
            atomic_json(output/f'{phase}-cases.json',entries)
            observations=[r['additional'] for r in entries]
            if phase=='development':
                policy=exact_policy(observations)
                atomic_json(output/'policy.json',policy)
            results[phase]={'independent':aggregate(entries),'add_all':measure(observations,'all',0.),
                           'whole':measure(observations,'whole',0.),
                           'by_policy':{key:measure(observations,key,cfg['cutoff']) for key,cfg in policy.items()
                                        if cfg['cutoff'] is not None}}
            if results[phase]['whole'] != previous['scores'][phase]['whole']:
                raise ValueError('冻结全图基线未复现')
        resources=usage.sample()
    summary={'state':'completed','recorded_utc':now(),'scores':results,'policy':policy,'resources':resources,
             'test_accessed':False,'deployed':False,'protocol_sha256':digest(output/'protocol.json'),
             'model_sha256':digest(output/'direct.pt'),'cases_sha256':{k:digest(output/f'{k}-cases.json') for k in results}}
    atomic_json(output/'summary.json',summary)
    atomic_json(output/'status.json',{'state':'completed','summary_sha256':digest(output/'summary.json')})
    return summary


if __name__=='__main__':
    configure_environment()
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('source','release','early','development','later','prior','base','output'):
        parser.add_argument('--'+key,type=Path,required=True)
    result=run(**vars(parser.parse_args()))
    print(json.dumps({'state':result['state'],'policy':result.get('policy')},ensure_ascii=False))
