"""Measure paired 1k NRT and definitive HMI LOS maps without relaxing quality gates."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import re

import numpy as np

from .ar_hmi_registration import fits_field, source_grid
from .durable import atomic_json
from .hmi_angular_projection import angular_sample, wcs
from .hmi_source_audit import tai_utc
from .journal import read
from .runtime import digest, now, output_path


ISWA = 'https://iswa.gsfc.nasa.gov/iswa_data_tree/observation/solar/sdo/hmi-magnetogram_NRT-FITS/'
JSOC = 'https://jsoc1.stanford.edu/data/hmi/fits/'
NOMINAL = re.compile(r'\d{8}_\d{6}')


def path_identity(flavor, nominal):
    if not NOMINAL.fullmatch(nominal) or flavor not in ('nrt','definitive'):
        raise ValueError('物理磁图配对身份非法')
    t = datetime.strptime(nominal,'%Y%m%d_%H%M%S')
    filename = f'hmi.M_720s{"_nrt" if flavor == "nrt" else ""}.{nominal}_TAI.fits'
    return filename, (ISWA+f'{t:%Y/%m}/' if flavor=='nrt' else JSOC+f'{t:%Y/%m/%d}/')+filename


def differences(definitive, nrt, core):
    support = core & np.isfinite(definitive) & np.isfinite(nrt)
    if not support.any():
        raise ValueError('磁图没有共同有效区域')
    a,b = definitive[support],nrt[support]
    delta = b-a
    levels = {}
    for limit in (20,100,300):
        selected = abs(a)>=limit
        count = int(selected.sum())
        levels[str(limit)] = {'pixels':count,
                             'same_sign':float(np.mean(np.sign(a[selected])==np.sign(b[selected]))) if count else None,
                             'median_absolute_delta_gauss':float(np.median(abs(delta[selected]))) if count else None}
    return {'shared_fraction':int(support.sum())/int(core.sum()), 'shared_pixels':int(support.sum()),
            'pearson_r':float(np.corrcoef(a,b)[0,1]) if a.std() and b.std() else None,
            'absolute_delta_p50_p95_gauss':np.quantile(abs(delta),[.5,.95]).tolist(),
            'signed_delta_median_gauss':float(np.median(delta)),
            'unsigned_mean_gauss':[float(np.mean(abs(x))) for x in (a,b)], 'defined_field_bands':levels}


def run(dataset, captures, output):
    output = output_path(output,dataset)
    if (output/'summary.json').exists() or output.is_relative_to(captures.resolve()):
        raise ValueError('验证报告须位于独立目录')
    receipt = read(captures/'receipt.json')
    if receipt['state']!='captured':
        raise ValueError('原始磁图没有下载留证')
    if 'selection_sha256' in receipt:
        selected = captures/'selection.json'
        if digest(selected)!=receipt['selection_sha256'] or read(selected)['dates']!=receipt['dates_selected_before_download']:
            raise ValueError('预先固定的比较时刻身份不同')
    records = defaultdict(dict)
    for item in receipt['rows']:
        nominal,flavor = item['nominal_tai'],item['flavor']
        name,uri = path_identity(flavor,nominal)
        if (item['file']!=name or item['url']!=uri or flavor in records[nominal]
                or nominal not in receipt['dates_selected_before_download']):
            raise ValueError('留证中存在未登记或重复来源')
        records[nominal][flavor]=item
    if set(records)!=set(receipt['dates_selected_before_download']):
        raise ValueError('历史时刻清单改变')
    reports = []
    for nominal,items in sorted(records.items()):
        if set(items)!= {'nrt','definitive'} or any(i['status']!='captured' for i in items.values()):
            reports.append({'nominal_tai':nominal,'state':'incomplete'})
            continue
        mapped = {}
        for flavor,item in items.items():
            path = captures/item['file']
            if digest(path)!=item['sha256']:
                raise ValueError('原始磁图字节已变更')
            image, h = fits_field(path)
            source_grid(h)
            key = datetime.strptime(nominal,'%Y%m%d_%H%M%S').strftime('%Y.%m.%d_%H:%M:%S_TAI')
            if tai_utc(h['T_REC'])!=tai_utc(key):
                raise ValueError('磁图与名义TAI时刻不同')
            mapped[flavor]=(image,h)
        a,ha = mapped['definitive']
        b,hb = mapped['nrt']
        delta = float((tai_utc(hb['T_OBS'])-tai_utc(ha['T_OBS'])).total_seconds())
        if abs(delta)>60:
            raise ValueError('同一记录的实际观测时差过大')
        on_a,geometry = angular_sample(b,hb,ha,a.shape)
        y,x=np.indices(a.shape,dtype=float)
        lon,lat=wcs(ha).all_pix2world(x,y,0)
        dx=((lon+180.)%360.-180.)*3600
        core=np.hypot(dx,lat*3600) <= .80*float(ha['RSUN_OBS'])
        reports.append({'nominal_tai':nominal,'state':'compared','signed_nrt_minus_definitive_observation_seconds':delta,
                        'quality':{k:int(h['QUALITY']) for k,(_,h) in mapped.items()},'difference':differences(a,on_a,core),
                        'geometry':geometry})
    result={'state':'completed','recorded_utc':now(),'code_sha256':digest(__file__),
            'receipt_sha256':digest(captures/'receipt.json'),'reports':reports,
            'scope':'exploratory pairing of the same nominal JSOC records, central 80% radius',
            'nominal_record_range_tai':[min(records),max(records)] if records else [],
            'quality_gate_relaxed':False,'issued_forecast':False,'test_accessed':False}
    atomic_json(output/'summary.json',result)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for argument in ('dataset','captures','output'):
        parser.add_argument('--'+argument,type=Path,required=True)
    print(json.dumps(run(**vars(parser.parse_args())),ensure_ascii=False))
