"""Audit physical HMI candidates for the fixed16 CH training times; no regridding."""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import re
import warnings

import numpy as np

from .aia_pair_audit import eligible_training
from .aia_source_probe import ones_sum
from .datasets import SegmentationDataset
from .durable import atomic_json,commit
from .runtime import digest,now,output_path


def tai_utc(value):
    from astropy.time import Time
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}\.\d{2}\.\d{2}_\d{2}:\d{2}:\d{2}(?:\.\d+)?_TAI',value):
        raise ValueError('HMI时间缺少明确TAI标识或格式无效')
    date,clock,_=value.split('_')
    return Time(date.replace('.','-')+'T'+clock,format='isot',scale='tai').utc.to_datetime(timezone=timezone.utc)


def run(dataset,aia,output):
    from astropy.io import fits
    from ..web.historical_hmi import download_hmi_hourly
    dataset,aia=Path(dataset),Path(aia);output=output_path(output,dataset)
    source=json.loads((aia/'protocol.json').read_text(encoding='utf-8'))
    if source['manifest_sha256']!=digest(dataset/'manifest-hashes.json'):raise ValueError('冻结训练清单改变')
    data=SegmentationDataset(dataset,'CH','train');eligible_training([r['uri'] for r in source['rows']],data.rows)
    protocol={'scope':'same16 CH train times only; physical HMI source audit, no masks, regridding, model or forecast training',
        'source_protocol_sha256':digest(aia/'protocol.json'),'code_sha256':digest(__file__),
        'network_code_sha256':digest(Path(__file__).parents[1]/'web/historical_hmi.py'),
        'quality_policy':'Record full QUALITY flags. Only QUALITY=0 enters preliminary clean-input group; nonzero is review-required, not automatically scientifically unusable.',
        'time_gate_seconds':60,'training_allowed':False}
    output.mkdir(parents=True,exist_ok=True)
    if (output/'protocol.json').exists():
        if json.loads((output/'protocol.json').read_text(encoding='utf-8'))!=protocol:raise ValueError('HMI审计版本改变')
    else:atomic_json(output/'protocol.json',protocol)
    if (output/'summary.json').exists():raise ValueError('已完成报告不覆盖')
    results=[]
    for row in source['rows']:
        if (output/'PAUSE').exists():atomic_json(output/'status.json',{'state':'paused','completed':len(results),'total':16});return {'state':'paused'}
        image=data.source(row['uri'])
        if digest(image)!=row['sha256']:raise ValueError('本地训练输入改变')
        folder=output/'pairs'/row['id'];folder.mkdir(parents=True,exist_ok=True)
        try:
            nominal=image.stem[3:16]+'00';filename=f'hmi.M_720s.{nominal}_TAI.fits';path=folder/filename;meta_path=path.with_suffix('.source.json')
            if path.exists():
                meta=json.loads(meta_path.read_text(encoding='utf-8'))
                if digest(path)!=meta['sha256']:raise ValueError('HMI缓存身份改变')
            else:
                raw,meta=download_hmi_hourly(nominal)
                commit(path,lambda stream:stream.write(raw));meta.update(sha256=digest(path),bytes=len(raw),fetched_utc=now());atomic_json(meta_path,meta)
            previous=json.loads((aia/'pairs'/row['id']/'source-audit.json').read_text(encoding='utf-8'))
            aia193=next(s for s in previous['sources'] if s['channel']=='193')
            if not aia193['header']['T_OBS'].endswith('Z'):raise ValueError('AIA时刻缺少UTC标识')
            aia_time=datetime.fromisoformat(aia193['header']['T_OBS'].replace('Z','+00:00'))
            raw=path.read_bytes();integrity=[]
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                with fits.open(path,disable_image_compression=True,memmap=False) as hdus:
                    for h in hdus:
                        if 'CHECKSUM' not in h.header:continue
                        info=h.fileinfo();integrity.append(ones_sum(raw[info['hdrLoc']:info['datLoc']+info['datSpan']])==0xffffffff and h.verify_datasum()==1)
                with fits.open(path,memmap=False) as hdus:
                    h=next(h for h in hdus if h.data is not None and h.data.ndim==2)
                    keys=('BUNIT','QUALITY','QUALLEV1','T_REC','T_OBS','DATE','DATE-OBS','TELESCOP','INSTRUME','CONTENT','CADENCE','EXPTIME','INT_TIME',
                        'CRPIX1','CRPIX2','CDELT1','CDELT2','CRVAL1','CRVAL2','CROTA2','CTYPE1','CTYPE2','CUNIT1','CUNIT2','RSUN_OBS','DSUN_OBS','CRLN_OBS','CRLT_OBS')
                    header={k:h.header.get(k) for k in keys};shape=list(h.data.shape)
                    if shape!=[1024,1024] or header['BUNIT']!='Gauss' or header['CONTENT']!='MAGNETOGRAM':raise ValueError('物理单位、产品或1024网格不符')
                    observation=tai_utc(header['T_OBS']);delta=(observation-aia_time).total_seconds()
                    if not integrity or not all(integrity):raise ValueError('HMI原始HDU校验失败')
                    stats={'finite_fraction':float(np.isfinite(h.data).mean()),'min_gauss':float(np.nanmin(h.data)),'max_gauss':float(np.nanmax(h.data))}
                    history=list(h.header.get('HISTORY',[]));comments=list(h.header.get('COMMENT',[]))
                warn=[str(w.message) for w in caught]
            result={'uri':row['uri'],'state':'source_reviewed','sha256':digest(path),'header':header,'shape':shape,'raw_integrity_passed':True,
                'hmi_observation_utc':observation.isoformat(),'aia193_observation_utc':aia_time.isoformat(),'signed_hmi_minus_aia_seconds':delta,
                'temporal_gate_passed':abs(delta)<=60,'quality_zero':header['QUALITY']==0,'quality_hex':hex(header['QUALITY']) if isinstance(header['QUALITY'],int) else None,
                'history':history,'comments':comments,'warnings':warn,'stats':stats,'reprojected':False,'training_allowed':False}
        except Exception as exc:result={'uri':row['uri'],'state':'failed','error':str(exc),'quality_zero':False,'training_allowed':False}
        results.append(result);atomic_json(folder/'review.json',result)
        progress={'state':'auditing_physical_sources','completed':len(results),'total':16,'reviewed':sum(r['state']=='source_reviewed' for r in results),'quality_zero':sum(r['quality_zero'] for r in results),'training':False}
        atomic_json(output/'status.json',progress);print(json.dumps(progress),flush=True)
    report={'state':'completed','created_utc':now(),'protocol_sha256':digest(output/'protocol.json'),'results':results,
        'reviewed':sum(r['state']=='source_reviewed' for r in results),'quality_zero':sum(r['quality_zero'] for r in results),
        'limits':['Hourly source is4x boxcar-binned, disk-cropped, north-up; it is not the original4096 source used for numerical paper parity.',
            'Nonzero QUALITY flags need product-specific interpretation; no automatic quality whitelist or parameter use.',
            'HMI and AIA grid scales differ; no naive concatenation, magnetic flux calculation or claimed complete co-registration.',
            'TAI converted explicitly; file creation DATE is not evidence of historical real-time availability.',
            'No masks or non-training pixels read; no training or model publication.'],
        'training_allowed':False,'mask_read':False,'test_accessed':False,'calibration_accessed':False}
    atomic_json(output/'summary.json',report);atomic_json(output/'status.json',{'state':'completed','completed':16,'total':16,'training':False,'reviewed':report['reviewed'],'quality_zero':report['quality_zero']});return report


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment();p=argparse.ArgumentParser();p.add_argument('--dataset',required=True);p.add_argument('--aia',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    r=run(a.dataset,a.aia,a.output);print(json.dumps({k:r[k] for k in ('state','reviewed','quality_zero') if k in r}))
