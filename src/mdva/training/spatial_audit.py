"""Validation-only analysis of missed regions against apparent disk position/size."""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from .runtime import digest,now,output_path
from .durable import atomic_json


def apparent_disk(gray):
    """Fit the visible white-background disk; refuse nonmatching image domains."""
    gray=np.asarray(gray)
    if gray.ndim!=2 or min(gray.shape)<32:raise ValueError('不支持的图像尺寸')
    if np.mean(np.concatenate((gray[0],gray[-1],gray[:,0],gray[:,-1]))>=250)<.9:
        raise ValueError('缺少已核验的白色外背景，不能推定日面边界')
    labels,n=ndimage.label(gray<250)
    sizes=np.bincount(labels.ravel());sizes[0]=0
    if not n:raise ValueError('无可用日面')
    disk=ndimage.binary_fill_holes(labels==sizes.argmax())
    if not .45<disk.mean()<.85:raise ValueError('日面占比异常')
    y,x=np.nonzero(disk & ~ndimage.binary_erosion(disk))
    keep=np.ones(len(x),dtype=bool)
    for _ in range(3):
        a=np.column_stack((2*x[keep],2*y[keep],np.ones(keep.sum())))
        cx,cy,c=np.linalg.lstsq(a,x[keep]**2+y[keep]**2,rcond=None)[0]
        radius=float(np.sqrt(c+cx**2+cy**2))
        residual=np.abs(np.hypot(x-cx,y-cy)-radius)
        keep=residual<=max(2.,np.median(residual)*3)
        if keep.mean()<.85:raise ValueError('可见边界与圆拟合不一致')
    if not .4<radius/min(gray.shape)<.51 or np.percentile(residual,95)>2:
        raise ValueError('日面圆拟合质量不足')
    return {'cx':float(cx),'cy':float(cy),'radius':radius,'p95_boundary_residual_pixels':float(np.percentile(residual,95)),
            'basis':'white-background apparent disk fit, not WCS/physical coordinates'}


def summarize(rows):
    return {'reference_regions':len(rows),'no_overlap':sum(r['covered_fraction']==0 for r in rows),
        'no_overlap_fraction':float(np.mean([r['covered_fraction']==0 for r in rows])),
        'half_covered_fraction':float(np.mean([r['covered_fraction']>=.5 for r in rows]))}


def run(localization,output,dataset):
    from .datasets import read_frozen,resolve_uri
    localization,output,dataset=Path(localization),Path(output),Path(dataset)
    output=output_path(output,dataset)
    if output.exists() and any(output.iterdir()):raise ValueError('请使用新的审计版本目录')
    sources=json.loads((dataset/'sources.json').read_text(encoding='utf-8'))
    allowed={r['uri']:r for r in read_frozen(dataset,'segmentation-ready.jsonl') if r['eligible'] and r['split']=='validation'}
    hashes={r['uri']:r['sha256'] for r in read_frozen(dataset,'source-files.jsonl')}
    cases=json.loads((localization/'cases.json').read_text(encoding='utf-8'))
    results=[];unavailable=[]
    for row in cases:
        frozen=allowed.get(row['uri'])
        if not frozen:raise ValueError('只接受冻结验证图像')
        files=[resolve_uri(frozen[k],sources) for k in ('uri','mask_uri')]
        if any(digest(p)!=hashes[frozen[k]] for k,p in zip(('uri','mask_uri'),files)):raise ValueError('原资料变化')
        mask=np.load(files[1],allow_pickle=False)
        with Image.open(files[0]) as im:gray=np.asarray(im.convert('L'))
        if gray.shape!=mask.shape:raise ValueError('图像与掩膜尺寸不匹配')
        try:disk=apparent_disk(gray)
        except ValueError as exc:
            unavailable.append({'sample_id':row['sample_id'],'reason':str(exc)});continue
        labels=mask if row['category']=='AR' else ndimage.label(mask>0,structure=np.ones((3,3)))[0]
        ids=np.unique(labels);ids=ids[ids>0]
        if len(ids)!=len(row['reference']):raise ValueError('区域顺序不一致')
        for identifier,reference in zip(ids,row['reference']):
            y,x=np.nonzero(labels==identifier)
            rho=float(np.median(np.hypot(x-disk['cx'],y-disk['cy'])/disk['radius']))
            results.append({**reference,'run_id':row['run_id'],'sample_id':row['sample_id'],'uri':row['uri'],
                'instrument':row['instrument'],'year':row['year'],'median_apparent_radius':rho,
                'disk_position':'outer_r_ge0.8' if rho>=.8 else 'central_r_lt0.8','disk_fit':disk})
    groups=defaultdict(list)
    for row in results:groups[row['run_id']].append(row)
    report={'state':'completed','created_utc':now(),'localization_sha256':digest(localization/'cases.json'),
        'code_sha256':digest(__file__),'scope':'validation_only','unavailable_disk_fits':unavailable,
        'models':{name:{key:{group:summarize([r for r in rows if r[key]==group]) for group in sorted({r[key] for r in rows})}
                       for key in ('disk_position','size_bin','year','instrument')} for name,rows in groups.items()},
        'limitations':['apparent image radius is not heliographic latitude/longitude',
            'correlation with position/size does not establish causal failure mechanism',
            'failed fits stay explicitly unmeasured; no labels are changed or removed from model evaluation']}
    atomic_json(output/'summary.json',report);atomic_json(output/'regions.json',results)
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--localization',required=True);p.add_argument('--output',required=True);p.add_argument('--dataset',required=True)
    a=p.parse_args();print(json.dumps(run(a.localization,a.output,a.dataset),ensure_ascii=True))
