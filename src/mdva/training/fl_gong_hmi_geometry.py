"""Audit GONG H-alpha and JSOC LOS angular pairing before any FL science use."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import numpy as np
from astropy.io import fits
from scipy.ndimage import gaussian_filter, maximum_filter, minimum_filter

from .ar_hmi_registration import fits_field
from .durable import atomic_json, commit
from .hmi_angular_projection import angular_sample, wcs
from .hmi_nrt_intake import data_url
from .hmi_source_audit import tai_utc
from .journal import read
from .runtime import digest, now, output_path
from .xrs_coverage import utc


SOURCE = 'https://services.swpc.noaa.gov/products/gong/haf/'
NAME = re.compile(r'(\d{14})([BCLMTU])h\.fits\.fz')
SITES = {'B':'BB','C':'CT','L':'LE','M':'ML','T':'TD','U':'UD'}
GRID = ('CTYPE1','CTYPE2','CUNIT1','CUNIT2','CRPIX1','CRPIX2','CRVAL1','CRVAL2',
        'CDELT1','CDELT2','CROTA1','CROTA2')


def native_alpha(path):
    with fits.open(path, disable_image_compression=True, memmap=False) as hdus:
        if not any(h.header.get('ZIMAGE') is True for h in hdus) or any(h.verify_checksum() != 1 or h.verify_datasum() != 1 for h in hdus):
            raise ValueError('GONG 原始压缩 FITS 校验失败')
    with fits.open(path, memmap=False) as hdus:
        img = next(h for h in hdus if h.data is not None and h.data.ndim == 2)
        header = {k:img.header[k] for k in (*GRID,'TELESCOP','DATE-OBS','TIMESYS','WAVELNTH','SITE','SOLAR_R','SOLAR-B0',
                                           'SOLAR-L0','HAONBAND','HAERRCOD','ECLIPSE')}
        if (img.data.shape != (2048,2048) or header['TELESCOP'] != 'NSO-GONG' or header['TIMESYS'] != 'UTC'
                or not 6562.6 <= header['WAVELNTH'] <= 6563.0):
            raise ValueError('GONG 科研 Hα 产品身份不符合协议')
        return img.data.astype(np.float32), header


def reduced(header, factor=4):
    result = {k:header[k] for k in GRID}
    for i in (1,2):
        result['CRPIX'+str(i)] = (header['CRPIX'+str(i)]-.5)/factor+.5
        result['CDELT'+str(i)] = header['CDELT'+str(i)]*factor
    return result


def weak_pil(field, valid):
    weights = gaussian_filter(valid.astype(float), 2)
    smooth = gaussian_filter(np.where(valid,field,0.), 2)/np.maximum(weights,.01)
    good = weights >= .98
    # A robust LOS polarity proxy: both locally supported signs, near zero itself.
    above = maximum_filter(np.where(good,smooth,-np.inf), size=7) >= 15
    below = minimum_filter(np.where(good,smooth,np.inf), size=7) <= -15
    return valid & good & above & below & (abs(smooth) <= 8)


def run(dataset, gong, hmi, output):
    output = output_path(output,dataset)
    if output.exists() or any(output.is_relative_to(p.resolve()) for p in (gong,hmi)):
        raise ValueError('配准审计使用独立新目录')
    receipt, magnetic = (read(path) for path in (gong/'receipt.json',hmi/'summary.json'))
    index = gong/'index.html'
    pointer = hmi/'latest_fits_time.txt'
    uri,_ = data_url(pointer.read_bytes())
    fn = hmi/urlsplit(uri).path.rsplit('/',1)[-1]
    if (receipt['state']!='captured' or receipt['index_url']!=SOURCE or receipt['index_sha256']!=digest(index)
            or receipt['hmi_receipt_sha256']!=digest(hmi/'summary.json') or uri!=magnetic['data_url']
            or digest(fn)!=magnetic['fits_sha256'] or digest(pointer)!=magnetic['pointer_sha256']):
        raise ValueError('原始观测和接收时间记录不符')
    field, header = fits_field(fn)
    observed = tai_utc(header['T_OBS'])
    if observed!=utc(magnetic['observation_utc']) or header['QUALITY']!=magnetic['quality']:
        raise ValueError('磁图观测时刻、质量标记与收据不同')
    files = []
    for item in receipt['matches']:
        name = item['file']
        shape_id = NAME.fullmatch(name)
        path = gong/name
        if (not shape_id or item['url']!=SOURCE+name or name not in index.read_text(encoding='utf-8')
                or digest(path)!=item['sha256']):
            raise ValueError('Hα 文件名、来源、指纹不符合已存的官方目录')
        intensity, a = native_alpha(path)
        stamp = datetime.fromisoformat(a['DATE-OBS']).replace(tzinfo=timezone.utc)
        seconds = (stamp-observed).total_seconds()
        if (stamp.strftime('%Y%m%d%H%M%S')!=shape_id[1] or a['SITE']!=SITES[shape_id[2]] or abs(seconds)>receipt['pair_limit_seconds']
                or abs(seconds-item['nominal_delta_seconds'])>1e-6):
            raise ValueError('Hα 文件名时刻、台站或配对间隔不符')
        target = reduced(a)
        projected, adjustment = angular_sample(field,header,target,(512,512))
        yy,xx = np.indices(projected.shape,dtype=float)
        lon,lat = wcs(target).all_pix2world(xx,yy,0)
        angle_x = ((lon+180.)%360.-180.)*3600
        disk = np.hypot(angle_x,lat*3600) <= float(a['SOLAR_R'])*.75
        valid = disk & np.isfinite(projected)
        pil = weak_pil(projected,valid)
        source_image = intensity.reshape(512,4,512,4).mean(axis=(1,3), dtype=np.float32)
        destination = output / (name[:16]+'.npz')
        commit(destination,lambda f:np.savez_compressed(f,halpha=source_image,hmi_los_gauss=projected.astype(np.float32),
                                                        valid_center=valid,los_sign_transition=pil))
        files.append({'file':name,'site':a['SITE'],'observed_utc':stamp.isoformat(),'signed_halpha_minus_hmi_seconds':seconds,
                      'halpha_flags':{k:a[k] for k in ('HAONBAND','HAERRCOD','ECLIPSE')},'raw_checksum_passed':True,
                      'solar_radius_arcsec':[float(header['RSUN_OBS']),a['SOLAR_R']],
                      'apparent_latitude_deg':[float(header['CRLT_OBS']),a['SOLAR-B0']],
                      'target_wcs':target,'angular_diagnostics':adjustment,
                      'center_finite_fraction':int(valid.sum())/max(1,int(disk.sum())),
                      'los_sign_transition_pixels':int(pil.sum()),'raster_asset':destination.name,'raster_asset_sha256':digest(destination)})
    report = {'state':'angular_geometry_audited','recorded_utc':now(),'hmi_quality':magnetic['quality'],
              'hmi_observed_utc':observed.isoformat(),'pairs':files,'receipt_sha256':digest(gong/'receipt.json'),
              'code_sha256':digest(__file__),'scientifically_validated_registration':False,
              'qualified_for_training':False,'issued_forecast':False,
              'limits':['HMI resampled in angular WCS only; solar rotation, observer displacement and chromospheric height uncorrected',
                        'LOS sign-transition diagnostic is restricted to central 0.75 solar radius; it is not a radial-field neutral line',
                        'No FL annotations for these live frames. Quality and feature alignment require independent evidence.']}
    atomic_json(output/'summary.json',report)
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('dataset','gong','hmi','output'):
        p.add_argument('--'+name,type=Path,required=True)
    print(json.dumps(run(**vars(p.parse_args())),ensure_ascii=False))
