"""Diagnostic angular-WCS sampling of coeval HMI onto AIA; not physical calibration."""
from __future__ import annotations

import argparse,json,warnings
from pathlib import Path
import numpy as np
from scipy.ndimage import map_coordinates,zoom

from .durable import atomic_json,commit
from .runtime import digest,now,output_path


def wcs(header):
    from astropy.io import fits
    from astropy.wcs import WCS
    if (header.get('CTYPE1'),header.get('CTYPE2'))!=('HPLN-TAN','HPLT-TAN'):
        raise ValueError('仅接受日面投影角坐标HPLN/HPLT TAN')
    if header.get('CUNIT1')!='arcsec' or header.get('CUNIT2')!='arcsec':raise ValueError('角坐标单位必须明确为arcsec')
    needed=('CRPIX1','CRPIX2','CDELT1','CDELT2','CRVAL1','CRVAL2')
    if any(not np.isfinite(header[k]) for k in needed) or header['CDELT1']==0 or header['CDELT2']==0:raise ValueError('角坐标网格无效')
    # Only known WCS cards; legacy unrelated nan cards cannot normalize raw FITS.
    keys=(*needed,'CTYPE1','CTYPE2','CUNIT1','CUNIT2','CROTA1','CROTA2',
        'PC1_1','PC1_2','PC2_1','PC2_2','CD1_1','CD1_2','CD2_1','CD2_2')
    clean=fits.Header();clean['NAXIS']=2
    for key in keys:
        if key in header:clean[key]=header[key]
    return WCS(clean,naxis=2)


def angular_sample(field,source_header,target_header,target_shape):
    if np.asarray(field).ndim!=2:raise ValueError('磁图必须二维')
    source,target=wcs(source_header),wcs(target_header)
    y,x=np.indices(target_shape,dtype=float)
    longitude,latitude=target.all_pix2world(x,y,0)
    sx,sy=source.all_world2pix(longitude,latitude,0)
    coordinates=np.stack((sy,sx));finite=np.isfinite(field)
    numerator=map_coordinates(np.where(finite,field,0.).astype(float),coordinates,order=1,mode='constant',cval=0.,prefilter=False)
    support=map_coordinates(finite.astype(float),coordinates,order=1,mode='constant',cval=0.,prefilter=False)
    result=np.full(target_shape,np.nan)
    good=np.isfinite(sx)&np.isfinite(sy)&(support>=1-1e-8)
    result[good]=numerator[good]/support[good]
    # Geometry round trip measures numerical transform consistency, not feature truth.
    lon2,lat2=source.all_pix2world(sx,sy,0);rx,ry=target.all_world2pix(lon2,lat2,0)
    residual=np.hypot(rx-x,ry-y)
    return result,{'valid_fraction':float(good.mean()),'numerical_roundtrip_max_pixels':float(np.nanmax(residual)),
        'kernel':'bilinear with complete finite support required; missing/outside staysNaN',
        'source_pixel_scale_arcsec':[source_header['CDELT1'],source_header['CDELT2']],
        'target_pixel_scale_arcsec':[target_header['CDELT1'],target_header['CDELT2']]}


def local_geometry(field,aia_header):
    import cv2
    if field.shape!=(1024,1024):raise ValueError('本地显示几何只登记1024原网格')
    radius=float(aia_header['RSUN_OBS'])/float(aia_header['CDELT1']);half=int(radius*1.1)
    if not 300<radius<500:raise ValueError('太阳视半径无效')
    affine=np.float32([[1,0,512-float(aia_header['CRPIX1'])],[0,1,512-(1024-float(aia_header['CRPIX2']))]])
    moved=cv2.warpAffine(np.flip(field,axis=0),affine,(1024,1024),borderMode=cv2.BORDER_CONSTANT,borderValue=np.nan)
    cropped=moved[512-half:512+half,512-half:512+half]
    # Linear geometry only: preserve signed Gauss. No sqrt, clipping or /255.
    return zoom(cropped,(512/(2*half),)*2,order=1)


def run(dataset,aia,hmi,output):
    from astropy.io import fits
    from .datasets import SegmentationDataset
    from .aia_pair_audit import eligible_training
    from .hmi_source_audit import tai_utc
    from datetime import datetime
    dataset,aia,hmi=Path(dataset),Path(aia),Path(hmi);output=output_path(output,dataset)
    if output.exists():raise ValueError('角坐标检查使用新版本，不覆盖')
    source=json.loads((aia/'protocol.json').read_text(encoding='utf-8'))
    audited=json.loads((hmi/'summary.json').read_text(encoding='utf-8'))
    if audited['protocol_sha256']!=digest(hmi/'protocol.json') or audited['reviewed']!=16:raise ValueError('先完成HMI来源核验')
    data=SegmentationDataset(dataset,'CH','train');eligible_training([r['uri'] for r in source['rows']],data.rows)
    known={r['uri']:r for r in audited['results']};output.mkdir(parents=True)
    atomic_json(output/'protocol.json',{'created_utc':now(),'code_sha256':digest(__file__),'hmi_audit_sha256':digest(hmi/'summary.json'),
        'aia_protocol_sha256':digest(aia/'protocol.json'),'scope':'fixed16 train-only angular sampling; accept QUALITY0 and delta<=60s; no masks or physical flux calculation','training_allowed':False})
    results=[]
    for row in source['rows']:
        record=known[row['uri']]
        if not record['quality_zero'] or not record['temporal_gate_passed']:
            results.append({'uri':row['uri'],'state':'skipped_quality_review_required','QUALITY':record['header']['QUALITY']});continue
        image=data.source(row['uri'])
        if digest(image)!=row['sha256']:raise ValueError('训练原图改变')
        hmi_folder=hmi/'pairs'/row['id'];hmi_path=next(hmi_folder.glob('*.fits'))
        original=json.loads((aia/'pairs'/row['id']/'source-audit.json').read_text(encoding='utf-8'))
        source193=next(s for s in original['sources'] if s['channel']=='193');aia_path=aia/'pairs'/row['id']/source193['filename']
        if digest(hmi_path)!=record['sha256'] or digest(aia_path)!=source193['sha256']:raise ValueError('物理原图身份改变')
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with fits.open(hmi_path,memmap=False) as hdus:
                h=next(h for h in hdus if h.data is not None and h.data.ndim==2);field=h.data.copy();hh=h.header.copy()
            with fits.open(aia_path,memmap=False) as hdus:
                h=next(h for h in hdus if h.data is not None and h.data.ndim==2);ah=h.header
                needed=('CRPIX1','CRPIX2','CDELT1','CDELT2','CRVAL1','CRVAL2','CROTA2','CTYPE1','CTYPE2','CUNIT1','CUNIT2','RSUN_OBS')
                # Build only needed cards; no in-memory normalization of raw file.
                target={k:ah[k] for k in needed}
                for k in ('PC1_1','PC1_2','PC2_1','PC2_2','CD1_1','CD1_2','CD2_1','CD2_2'):
                    if k in ah:target[k]=ah[k]
        observed=tai_utc(hh['T_OBS']);stamp=datetime.fromisoformat(source193['header']['T_OBS'].replace('Z','+00:00'))
        if hh['BUNIT']!='Gauss' or hh['QUALITY']!=0 or abs((observed-stamp).total_seconds())>60:raise ValueError('物理单位、质量或时刻改变')
        projected,diagnostics=angular_sample(field,hh,target,(1024,1024));local=local_geometry(projected,target)
        folder=output/'pairs'/row['id'];folder.mkdir(parents=True)
        commit(folder/'hmi-on-aia-local512.npy',lambda stream:np.save(stream,local,allow_pickle=False))
        result={'uri':row['uri'],'state':'angular_sampled_not_physically_certified','BUNIT':'Gauss','QUALITY':0,
            'native_hmi_sha256':digest(hmi_path),'native_aia_sha256':digest(aia_path),'array_sha256':digest(folder/'hmi-on-aia-local512.npy'),
            'local_finite_fraction':float(np.isfinite(local).mean()),'local_minmax_gauss':[float(np.nanmin(local)),float(np.nanmax(local))],**diagnostics}
        atomic_json(folder/'review.json',result);results.append(result)
    report={'state':'completed','created_utc':now(),'protocol_sha256':digest(output/'protocol.json'),'results':results,
        'sampled':sum(r['state']=='angular_sampled_not_physically_certified' for r in results),'skipped':sum(r['state'].startswith('skipped') for r in results),
        'training_allowed':False,'mask_read':False,'test_accessed':False,'calibration_accessed':False,
        'limits':['Angular WCS numerical alignment only. Different observation times/observer geometry and solar rotation are not physically reprojected.',
            'Local raster follows supplied display geometry with unresolved white outer background; no label alignment certification.',
            'B_LOS remains B_LOS: no radial-field inference, physical total flux, polarity judgment or full CHIMERA result.']}
    atomic_json(output/'summary.json',report);return report


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment();p=argparse.ArgumentParser();p.add_argument('--dataset',required=True);p.add_argument('--aia',required=True);p.add_argument('--hmi',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    r=run(a.dataset,a.aia,a.hmi,a.output);print(json.dumps({'state':r['state'],'sampled':r['sampled'],'skipped':r['skipped']}))
