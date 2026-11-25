"""Complete the EUV source triplet for fixed training cases; no model training."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import warnings

from .aia_pair_audit import eligible_training
from .aia_source_probe import ones_sum
from .datasets import SegmentationDataset
from .durable import atomic_json, commit
from .runtime import digest, now, output_path

GRID = ('CRPIX1','CRPIX2','CDELT1','CDELT2','CRVAL1','CRVAL2','CROTA2','CTYPE1','CTYPE2','CUNIT1','CUNIT2')


def compatibility(header, reference):
    if header['WAVELNTH'] != 171 or header['QUALITY'] != 0 or header['LVL_NUM'] != 1.5:
        raise ValueError('171通道、质量或处理级别不符')
    if not header['T_OBS'].endswith('Z') or not reference['T_OBS'].endswith('Z'):
        raise ValueError('观测时刻必须具有UTC标识')
    stamps=[datetime.fromisoformat(h['T_OBS'].replace('Z','+00:00')) for h in (header,reference)]
    delta=(stamps[0]-stamps[1]).total_seconds()
    return {'same_wcs_grid_fields':all(header[k]==reference[k] for k in GRID),
        'signed_171_minus193_seconds':delta,
        'source_gate_passed':all(header[k]==reference[k] for k in GRID) and abs(delta)<=60}


def source_rows(protocol, summary, allow_partial):
    rows = protocol['rows']
    reviewed = {r['uri']: r for r in summary['results']}
    if (summary['state'] != 'completed' or len(summary['results']) != len(rows)
            or set(reviewed) != {r['uri'] for r in rows}):
        raise ValueError('193/211审计未完整覆盖冻结清单')
    passed = {uri for uri, r in reviewed.items() if r.get('alignment_review_gate_passed') is True}
    if not passed or len(passed) != summary['source_review_passed'] or not allow_partial and len(passed) != len(rows):
        raise ValueError('先完成193/211来源核验')
    return [(r, None if r['uri'] in passed else reviewed[r['uri']].get('error') or '193/211配准门槛未通过') for r in rows]


def run(dataset, previous, output, allow_partial=False):
    from astropy.io import fits
    from ..web.space_weather import download_aia_synoptic
    dataset,previous=Path(dataset),Path(previous);output=output_path(output,dataset)
    source_protocol=json.loads((previous/'protocol.json').read_text(encoding='utf-8'))
    source_summary=json.loads((previous/'summary.json').read_text(encoding='utf-8'))
    if source_summary['protocol_sha256']!=digest(previous/'protocol.json'):
        raise ValueError('193/211来源协议身份不符')
    if source_protocol['manifest_sha256']!=digest(dataset/'manifest-hashes.json'):raise ValueError('冻结清单改变')
    data=SegmentationDataset(dataset,'CH','train');eligible_training([r['uri'] for r in source_protocol['rows']],data.rows)
    inputs = source_rows(source_protocol, source_summary, allow_partial)
    protocol={'scope':'same16 fixed CH train images; only171 additional official files; no masks/test/calibration',
        'source_protocol_sha256':digest(previous/'protocol.json'),'code_sha256':digest(__file__),
        'network_code_sha256':digest(Path(__file__).parents[1]/'web/space_weather.py'),
        'multichannel_training_allowed':False,'missing_for_full_CHIMERA':['co-registered physical HMI LOS magnetogram','verified whole-image geometry and preprocessing','validated original algorithm execution']}
    if allow_partial:
        protocol.update(allow_missing_193_211=True, source_summary_sha256=digest(previous/'summary.json'))
    output.mkdir(parents=True,exist_ok=True)
    if (output/'protocol.json').exists():
        if json.loads((output/'protocol.json').read_text(encoding='utf-8'))!=protocol:raise ValueError('171资料审计版本改变')
    else:atomic_json(output/'protocol.json',protocol)
    if (output/'summary.json').exists():raise ValueError('已完成审计不覆盖')
    results=[]
    for row, unavailable in inputs:
        if (output/'PAUSE').exists():atomic_json(output/'status.json',{'state':'paused','completed':len(results),'total':16});return {'state':'paused'}
        if unavailable is not None:
            results.append({'uri':row['uri'], 'state':'skipped_missing_193_211', 'reason':unavailable,
                            'source_gate_passed':False})
            continue
        source=data.source(row['uri'])
        if digest(source)!=row['sha256']:raise ValueError('训练输入指纹改变')
        folder=output/'pairs'/row['id'];folder.mkdir(parents=True,exist_ok=True)
        try:
            nominal=source.stem[3:16];path=folder/f'AIA{nominal}_0171.fits';meta_path=path.with_suffix('.source.json')
            if path.exists():
                meta=json.loads(meta_path.read_text(encoding='utf-8'))
                if meta['sha256']!=digest(path):raise ValueError('171缓存身份改变')
            else:
                raw,meta=download_aia_synoptic(nominal,'171')
                commit(path,lambda stream:stream.write(raw));meta.update(sha256=digest(path),bytes=len(raw),fetched_utc=now());atomic_json(meta_path,meta)
            reference_folder=previous/'pairs'/row['id']
            mapping=json.loads((reference_folder/'mapping-review.json').read_text(encoding='utf-8'))
            if mapping['source_audit_sha256']!=digest(reference_folder/'source-audit.json'):raise ValueError('193来源审计改变')
            source193=next(s for s in json.loads((reference_folder/'source-audit.json').read_text(encoding='utf-8'))['sources'] if s['channel']=='193')
            reference={**source193['header'],**{k:v['193'] for k,v in mapping['wcs_grid_metadata'].items()}}
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always');raw=path.read_bytes();checks=[]
                with fits.open(path,disable_image_compression=True,memmap=False) as hdus:
                    for h in hdus:
                        if 'CHECKSUM' not in h.header:continue
                        info=h.fileinfo();checks.append(ones_sum(raw[info['hdrLoc']:info['datLoc']+info['datSpan']])==0xffffffff and h.verify_datasum()==1)
                with fits.open(path,memmap=False) as hdus:
                    h=next(h for h in hdus if h.data is not None and h.data.ndim==2)
                    if h.data.shape!=(1024,1024):raise ValueError('171图像网格不符')
                    header={k:h.header.get(k) for k in (*GRID,'WAVELNTH','QUALITY','LVL_NUM','T_OBS')}
                warning_messages=[str(w.message) for w in caught]
            if not checks or not all(checks):raise ValueError('171原始HDU校验失败')
            result={'uri':row['uri'],'state':'source_reviewed','header':header,'sha256':digest(path),
                'raw_integrity_passed':True,'warnings':warning_messages,**compatibility(header,reference)}
        except Exception as exc:result={'uri':row['uri'],'state':'failed','error':str(exc),'source_gate_passed':False}
        results.append(result);atomic_json(folder/'review.json',result)
        state={'state':'auditing_sources','completed':len(results),'total':16,'passed':sum(r['source_gate_passed'] for r in results),'training':False}
        atomic_json(output/'status.json',state);print(json.dumps(state),flush=True)
    report={'state':'completed','created_utc':now(),'protocol_sha256':digest(output/'protocol.json'),
        'results':results,'passed':sum(r['source_gate_passed'] for r in results),'training_allowed':False,
        'mask_read':False,'test_accessed':False,'calibration_accessed':False,
        'scope':'EUV source triplet viability only; not full CHIMERA reproduction or permission for training; definitive products are not contemporaneously available for forecasting.'}
    atomic_json(output/'summary.json',report);atomic_json(output/'status.json',{'state':'completed','completed':16,'total':16,'passed':report['passed'],'training':False});return report


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment();p=argparse.ArgumentParser();p.add_argument('--dataset',required=True);p.add_argument('--previous',required=True);p.add_argument('--output',required=True);p.add_argument('--allow-partial',action='store_true');a=p.parse_args()
    r=run(a.dataset,a.previous,a.output,a.allow_partial);print(json.dumps({'state':r['state'],'passed':r.get('passed')}))
