"""Train-only diagnostic scan of source geometry; never corrects production inputs."""
from __future__ import annotations

import argparse
import json
import warnings
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import shift

from .aia_pair_audit import eligible_training
from .datasets import SegmentationDataset
from .durable import atomic_json
from .runtime import digest, now, output_path


def compare(gray, rendered):
    if gray.shape != (512,512) or rendered.shape != gray.shape:
        raise ValueError('只允许登记的512网格')
    y,x=np.ogrid[:512,:512]; radius=np.hypot(x-255.5,y-255.5)
    gray, rendered=gray.astype(float),rendered.astype(float)
    rings=[]
    for lower,upper in ((0,180),(180,200),(200,220),(220,228),(228,234),(234,240),(240,250),(250,363)):
        selected=(radius>=lower)&(radius<upper)
        a,b=gray[selected],rendered[selected]
        rings.append({'radius_range':[lower,upper],'pixels':int(selected.sum()),
            'local_white_fraction_ge250':float((a>=250).mean()),'mae_8bit':float(np.abs(a-b).mean()),
            'correlation':float(np.corrcoef(a,b)[0,1]) if a.std()>0 and b.std()>0 else None})
    # A bounded posthoc geometry diagnostic, not an input transform or an optimizer.
    core=radius<200; scan=[]
    for dy in (-1.,-.5,0.,.5,1.):
        for dx in (-1.,-.5,0.,.5,1.):
            sampled=shift(rendered,(dy,dx),order=1,prefilter=False,mode='constant',cval=np.nan)
            scan.append({'dx':dx,'dy':dy,'core_mae_8bit':float(np.abs(gray[core]-sampled[core]).mean())})
    best=min(scan,key=lambda v:(v['core_mae_8bit'],abs(v['dx'])+abs(v['dy'])))
    return {'rings':rings,'shift_scan':scan,'minimum_mae_shift':best,'shift_applied':False}


def run(dataset, audit, output):
    from astropy.io import fits
    from .aia_source_probe import mapping as render
    dataset,audit=Path(dataset),Path(audit)
    output=output_path(output,dataset)
    if output.exists():raise ValueError('几何诊断使用新版本，不覆盖')
    p=json.loads((audit/'protocol.json').read_text(encoding='utf-8'))
    summary=json.loads((audit/'summary.json').read_text(encoding='utf-8'))
    if summary['protocol_sha256']!=digest(audit/'protocol.json') or summary['source_review_passed']!=16:
        raise ValueError('先完成固定16组来源复核')
    if p['manifest_sha256']!=digest(dataset/'manifest-hashes.json'):raise ValueError('冻结清单改变')
    data=SegmentationDataset(dataset,'CH','train')
    eligible_training([r['uri'] for r in p['rows']],data.rows)
    output.mkdir(parents=True)
    protocol={'created_utc':now(),'source_protocol_sha256':digest(audit/'protocol.json'),
        'code_sha256':digest(__file__),'selection':'same16 fixed training images; no masks/validation/calibration/test',
        'scan':'all25 dy/dx combinations in -1,-0.5,0,0.5,1 pixels, fixed core radius200 centered255.5; posthoc geometry diagnostic only',
        'original_inputs_changed':False,'automatic_multichannel_training':False}
    atomic_json(output/'protocol.json',protocol)
    results=[]
    for row in p['rows']:
        source=data.source(row['uri'])
        if digest(source)!=row['sha256']:raise ValueError('原图身份改变')
        pair=audit/'pairs'/row['id']
        mapping=json.loads((pair/'mapping-review.json').read_text(encoding='utf-8'))
        if mapping['source_audit_sha256']!=digest(pair/'source-audit.json'):raise ValueError('原映射来源改变')
        gray=np.asarray(Image.open(source).convert('L'))
        rendered=np.asarray(Image.open(pair/'fixed-mapping-193.png').convert('L'))
        sources=json.loads((pair/'source-audit.json').read_text(encoding='utf-8'))
        source193=next(s for s in sources['sources'] if s['channel']=='193')
        raw=(pair/source193['filename']).resolve()
        if raw.parent!=pair.resolve() or digest(raw)!=source193['sha256']:raise ValueError('FITS来源身份改变')
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with fits.open(raw,memmap=False) as hdus:
                h=next(h for h in hdus if h.data is not None and h.data.ndim==2)
                expected,_=render(h.data,{k:h.header[k] for k in ('CDELT1','RSUN_OBS','CRPIX1','CRPIX2')})
        if not np.array_equal(expected,rendered):raise ValueError('映射图不能从冻结FITS复算')
        result={'uri':row['uri'],'source_sha256':digest(source),
            'mapped_image_sha256':digest(pair/'fixed-mapping-193.png'),
            'physical_solar_radius_after_supplied_crop':mapping['mapping']['physical_radius_pixels']*512/(2*mapping['mapping']['crop_half_pixels']),
            **compare(gray,rendered)}
        results.append(result);atomic_json(output/(row['id']+'.json'),result)
    report={'state':'completed','created_utc':now(),'protocol_sha256':digest(output/'protocol.json'),'images':16,'results':results,
        'limits':['Shift scan is posthoc source geometry diagnosis, not corrected WCS or label alignment certification.',
            'Fixed mapping uses supplied arithmetic and approximate gray LUT; source compression and JPEG differences remain.',
            'Outer white background procedure remains unknown. No inferred disk mask is used to alter inputs.',
            'No masks or non-training image pixels read; no new model or multichannel training authorized.'],
        'training_allowed':False,'mask_read':False,'test_accessed':False,'calibration_accessed':False}
    atomic_json(output/'summary.json',report)
    return report


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment();p=argparse.ArgumentParser();p.add_argument('--dataset',required=True);p.add_argument('--audit',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    r=run(a.dataset,a.audit,a.output);print(json.dumps({'state':r['state'],'images':r['images']}))
