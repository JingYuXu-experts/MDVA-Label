"""Evaluate archived SHARP patches as AR weak-label priors on the native display grid."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import re
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

import numpy as np
from scipy.ndimage import map_coordinates

from .aia_source_probe import ones_sum
from .ar_hmi_candidate_neighbors import checked_frame
from .ar_hmi_registration import DISPLAY_RADIUS, FILENAME, SIZE, correlation
from .ar_scale_review import native_objects
from .datasets import SegmentationDataset
from .durable import atomic_json, commit
from .journal import read
from .runtime import digest, now, output_path


INDEX = 'http://jsoc.stanford.edu/cgi-bin/ajax/jsoc_info'
ARCHIVE = 'https://jsoc1.stanford.edu'
KEYWORDS = ('HARPNUM', 'T_REC', 'QUALITY', 'NOAA_ARS', 'CTYPE1', 'CTYPE2', 'CUNIT1', 'CUNIT2',
            'CRPIX1', 'CRPIX2', 'CRVAL1', 'CRVAL2', 'CDELT1', 'CDELT2', 'CROTA2', 'DATE',
            'RSUN_OBS', 'IMCRPIX1', 'IMCRPIX2')
FITS_SEGMENT = re.compile(r'/SUM\d+/D\d+/S\d+/(bitmap|magnetogram)\.fits')
SEGMENTS = {'bitmap': 5 * 1024 * 1024, 'magnetogram': 24 * 1024 * 1024}
MIN_CORRELATION = .90
MIN_ALIGNMENT_MARGIN = .10


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def capture(url, path, limit):
    receipt = path.with_suffix(path.suffix+'.source.json')
    if path.is_file() and receipt.is_file():
        meta = read(receipt)
        if meta['url'] != url or meta['sha256'] != digest(path):
            raise ValueError('SHARP缓存内容或来源改变')
        return meta
    with build_opener(NoRedirect()).open(Request(url, headers={'User-Agent': 'MDVA local SHARP research'}), timeout=16) as response:
        raw = response.read(limit+1)
    if len(raw) > limit:
        raise ValueError('SHARP响应大小超出上限')
    commit(path, lambda stream: stream.write(raw))
    meta = {'url': url, 'sha256': digest(path), 'fetched_utc': now(), 'bytes': len(raw)}
    atomic_json(receipt, meta)
    return meta


def catalog(stamp, folder):
    timestamp = stamp.strftime('%Y.%m.%d_%H:%M:%S_TAI')
    query = urlencode({'op': 'rs_list', 'ds': f'hmi.sharp_720s[][{timestamp}]',
                       'key': ','.join(KEYWORDS), 'seg': ','.join(SEGMENTS)})
    path = folder/'catalog.json'
    receipt = capture(INDEX+'?'+query, path, 512*1024)
    index = read(path)
    if index['status'] != 0 or not 0 <= index['count'] <= 100:
        raise ValueError('SHARP目录状态无效')
    count = index['count']
    if not count:
        return [], receipt
    keywords = {entry['name']: entry['values'] for entry in index['keywords']}
    segments = {entry['name']: entry for entry in index['segments']}
    if set(keywords) != set(KEYWORDS) or set(segments) != set(SEGMENTS):
        raise ValueError('SHARP目录字段缺失')
    if any(len(value) != count for value in keywords.values()) or len(set(keywords['HARPNUM'])) != count:
        raise ValueError('SHARP目录结果长度或编号异常')
    records = []
    for i in range(count):
        row = {key: values[i] for key, values in keywords.items()}
        if row['T_REC'] != timestamp or not re.fullmatch(r'\d{1,8}', row['HARPNUM']):
            raise ValueError('SHARP索引并非所请求时刻')
        files = {}
        for name, segment in segments.items():
            relative = segment['values'][i]
            match = FITS_SEGMENT.fullmatch(relative)
            dim = segment['dims'][i]
            if match and match[1] == name and re.fullmatch(r'\d{1,4}x\d{1,4}', dim):
                w, h = map(int, dim.split('x'))
                if 0 < w <= 4096 and 0 < h <= 4096:
                    files[name] = {'url': ARCHIVE+relative, 'shape': [h,w]}
        records.append({'metadata': row, 'files': files})
    return records, receipt


def read_fits(path, expected, bitmap):
    from astropy.io import fits

    raw = path.read_bytes()
    with fits.open(path, disable_image_compression=True, memmap=False) as hdus:
        checks = []
        for hdu in hdus:
            if 'CHECKSUM' in hdu.header:
                info = hdu.fileinfo()
                section = raw[info['hdrLoc']:info['datLoc']+info['datSpan']]
                checks.append(ones_sum(section) == 0xffffffff and hdu.verify_datasum() == 1)
    if not checks or not all(checks):
        raise ValueError('SHARP原始FITS校验失败')
    with fits.open(path, memmap=False) as hdus:
        arrays = [hdu.data for hdu in hdus if hdu.data is not None and hdu.data.ndim == 2]
    if len(arrays) != 1 or list(arrays[0].shape) != expected:
        raise ValueError('SHARP位图或磁图与目录尺寸不符')
    return bitmap_codes(arrays[0]) if bitmap else arrays[0]


def bitmap_codes(result):
    # The archive stores exact integer flags in a floating-point FITS container.
    if np.any(~np.isin(result, [0,1,2,33,34])):
        raise ValueError('SHARP位图存在未知值')
    return result.astype(np.uint8)


def patch_coordinates(metadata, full_header, registration, x, y):
    if (tuple(metadata[key] for key in ('CTYPE1','CTYPE2','CUNIT1','CUNIT2')) != ('HPLN-TAN','HPLT-TAN','arcsec','arcsec')
            or any(abs(float(metadata[key])) > .001 for key in ('CRVAL1','CRVAL2'))):
        raise ValueError('仅接受零参考角的标准SHARP CCD坐标')
    radius = float(metadata['RSUN_OBS'])
    dx, dy, scale = registration['adjustment_xy_scale']
    sx, sy, roll, px, py = map(float, (metadata[k] for k in ('CDELT1','CDELT2','CROTA2','CRPIX1','CRPIX2')))
    if (not np.isfinite([radius,sx,sy,roll,px,py]).all()
            or abs(radius-float(full_header['RSUN_OBS'])) > .5 or min(sx,sy) < .45 or max(sx,sy) > .56):
        raise ValueError('SHARP与同刻全日面磁图的比例或视半径不符')
    roll = np.deg2rad(roll)
    sign_x, sign_y = registration['orientation']
    tx = (x-(SIZE-1)/2-dx)*sign_x*radius*scale/DISPLAY_RADIUS
    ty = (y-(SIZE-1)/2-dy)*sign_y*radius*scale/DISPLAY_RADIUS
    return np.stack((py-1+(-tx*np.sin(roll)+ty*np.cos(roll))/sy,
                     px-1+(tx*np.cos(roll)+ty*np.sin(roll))/sx))


def compare_magnetic_fields(current, patch, coordinates, coverage):
    first = np.clip(current[coverage], -500, 500)
    locations = coordinates[:,coverage]
    cardinal = ((0,0),(-20,0),(20,0),(0,-20),(0,20))
    alternatives = []
    for nudge in cardinal:
        moved = locations + np.asarray(nudge)[:,None]
        sampled = map_coordinates(patch, moved, order=1, mode='constant', cval=np.nan, prefilter=False)
        score = correlation(first, np.clip(sampled,-500,500))
        alternatives.append(score)
    exact, *other = alternatives
    competitors = [x for x in other if x is not None]
    margin = exact-max(competitors) if exact is not None and competitors else None
    return {'same_observation_correlation': exact, 'alignment_margin':margin,
            'controls': alternatives[1:], 'sample_pixels':int(np.count_nonzero(coverage)),
            'accepted': bool(exact is not None and exact >= MIN_CORRELATION
                             and margin is not None and margin >= MIN_ALIGNMENT_MARGIN)}


def intake(stamp, folder, with_magnetogram):
    records, receipt = catalog(stamp, folder)
    for record in records:
        if int(record['metadata']['QUALITY'], 16) != 0:
            record['skipped'] = 'source_quality'
            continue
        for kind in ('bitmap','magnetogram') if with_magnetogram else ('bitmap',):
            if kind not in record['files']:
                record['skipped'] = 'segment_unavailable'
                break
            field = record['files'][kind]
            name = f"{record['metadata']['HARPNUM']}-{kind}.fits"
            try:
                field.update(capture(field['url'], folder/name, SEGMENTS[kind]), filename=name)
            except (OSError,ValueError) as exc:
                record.update(skipped='source_unavailable',reason=str(exc))
                break
    return records,receipt


def evaluate_patch(record, folder, current, header, registration, with_magnetogram):
    blank = np.zeros(current.shape,dtype=bool)
    if 'skipped' in record:
        return blank, {'state':record['skipped']}
    fields = record['files']
    raw = read_fits(folder/fields['bitmap']['filename'], fields['bitmap']['shape'], True)
    if not np.any(raw & 32):
        return blank, {'state':'empty_tracked_patch'}
    y,x = np.indices(current.shape)
    coordinates = patch_coordinates(record['metadata'],header,registration,x,y)
    overlay = map_coordinates(raw,coordinates,order=0,mode='constant',cval=0,prefilter=False)
    region = ((overlay & 32) != 0) & np.isfinite(current)
    if not region.any():
        return blank, {'state':'patch_outside_display'}
    strength = {'patch_pixels':int(region.sum()), 'strong_patch_pixels':int((region & (overlay==34)).sum())}
    if not with_magnetogram:
        return region, {'state':'projected',**strength}
    patch = read_fits(folder/fields['magnetogram']['filename'], fields['magnetogram']['shape'],False).astype(np.float32)
    if patch.shape != raw.shape:
        raise ValueError('位图和对照磁图不是同一裁切网格')
    alignment = compare_magnetic_fields(current,patch,coordinates,region)
    return region if alignment['accepted'] else blank, {
        'state':'co_registered' if alignment['accepted'] else 'alignment_uncertain', **strength, **alignment}


def select_inputs(scope, dataset, groups, baseline):
    reference = {row['uri']:row for row in dataset.rows}
    available = {}
    for group in groups:
        report = read(group/'summary.json')
        plan = read(group/'protocol.json')
        if (report['state'] != 'completed' or report['test_accessed'] or report['calibration_accessed']
                or report['protocol_sha256'] != digest(group/'protocol.json')
                or (plan.get('dataset_sha256') or plan['data']['manifest-hashes.json']) != scope):
            raise ValueError('队列不能用于开发集SHARP评价')
        for row in report['results']:
            if row['split'] != 'validation':
                continue
            if row['uri'] not in reference or row['uri'] not in baseline or row['uri'] in available:
                raise ValueError('开发图像清单对应关系异常')
            if row['state'] == 'display_alignment_supported':
                available[row['uri']] = (group,row,reference[row['uri']])
    return sorted(available.values(),key=lambda item: item[1]['uri'])


def coverage(mask, instance, baseline):
    ids,sizes = np.unique(instance[instance>0],return_counts=True)
    hit,counts = np.unique(instance[mask & (instance>0)],return_counts=True)
    inside = dict(zip(hit.tolist(),counts.tolist(),strict=True))
    absent = list(set(ids.tolist())-set(baseline['matched_reference_ids']))
    return [{'instance':int(rid),'area':int(size),'baseline_missing':int(rid) in absent,
             'harp_coverage':inside.get(int(rid),0)/int(size)}
            for rid,size in zip(ids,sizes,strict=True)]


def run(dataset, baseline, cohorts, output, days=None, count=24):
    dataset = Path(dataset).resolve()
    baseline, output = Path(baseline).resolve(), output_path(output,dataset)
    cohorts = [Path(path).resolve() for path in cohorts]
    if (output/'summary.json').is_file() or any(output.is_relative_to(path) for path in (baseline,*cohorts)):
        raise ValueError('SHARP研究需使用独立输出版本')
    frozen = read(baseline/'summary.json')
    if frozen['state'] != 'completed' or frozen['test_accessed'] or frozen['calibration_accessed'] or frozen['images_sha256'] != digest(baseline/'images.json'):
        raise ValueError('固定活动区候选结果身份不符')
    prior = {row['uri']:row['baseline'] for row in read(baseline/'images.json')}
    data = SegmentationDataset(dataset,'AR','validation')
    all_inputs = select_inputs(digest(dataset/'manifest-hashes.json'),data,cohorts,prior)
    if days:
        picked = [row for row in all_inputs if row[1]['source']['nominal_tai'][:8] in days]
        if len(picked) != len(days):
            raise ValueError('指定开发日期缺少已配准磁图')
    else:
        indices = np.random.default_rng(2026).choice(len(all_inputs),size=count,replace=False)
        picked = [all_inputs[int(i)] for i in sorted(indices)]
    policy = {'selection':[p[1]['uri'] for p in picked], 'model_baseline_sha256':digest(baseline/'summary.json'),
              'cohort_summary_sha256':[digest(p/'summary.json') for p in cohorts],'source':'hmi.sharp_720s definitive',
              'minimum_alignment_correlation': MIN_CORRELATION, 'minimum_alignment_margin':MIN_ALIGNMENT_MARGIN,
              'fit_on_reference':False,'code_sha256':digest(__file__),'test_accessed':False,'calibration_accessed':False}
    checked = output/'protocol.json'
    if checked.is_file() and read(checked) != policy:
        raise ValueError('原有接入实验参数改变')
    atomic_json(checked,policy)
    def retrieve(entry):
        _, item, _ = entry
        stamp = datetime.strptime(FILENAME.fullmatch(Path(item['uri']).name)[1],'%Y%m%d_%H%M%S')
        folder = output/'sources'/item['source']['nominal_tai']
        try:
            records, receipt = intake(stamp,folder,True)
            return stamp,folder,records,receipt
        except (OSError,ValueError,KeyError) as exc:
            return stamp,folder,[],{'reason':str(exc)}
    rows = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        fetched = pool.map(retrieve,picked)
        for (group,item,source),(stamp,folder,records,receipt) in zip(picked,fetched,strict=True):
            original = group/'pairs'/item['id']
            _,header,_,observed = checked_frame(original/item['source']['filename'], item['source'],stamp)
            path = original/'aligned-los.npy'
            if digest(path) != item['array_sha256']:
                raise ValueError('全日面地理配准缓存不符')
            field = np.load(path,allow_pickle=False)
            combined = np.zeros(field.shape,dtype=bool)
            audit = []
            for record in records:
                try:
                    region,evidence = evaluate_patch(record,folder,field,header,item['registration'],True)
                    combined |= region
                except (OSError,ValueError,KeyError) as exc:
                    evidence = {'state':'invalid_patch','reason':str(exc)}
                audit.append({**record,**evidence})
            reference = np.load(data.source(source['mask_uri']),allow_pickle=False)
            row = {'uri':item['uri'],'time_utc':observed.isoformat(),'catalog':receipt,'patches':audit,
                   'instances':coverage(combined,reference,prior[item['uri']]),
                   'objects':native_objects(combined,reference)}
            rows.append(row)
            atomic_json(output/'progress.json',{'state':'measuring','done':len(rows),'total':len(picked)})
    atomic_json(output/'images.json',rows)
    measured = [row for row in rows if any(p['state']=='co_registered' for p in row['patches'])]
    missing = [r for row in measured for r in row['instances'] if r['baseline_missing']]
    recovered = sum(r['harp_coverage'] >= .5 for r in missing)
    counts = {'images':len(rows),'co_registered_images':len(measured),
              'patch_states':dict(Counter(p['state'] for row in rows for p in row['patches'])),
              'baseline_missed_regions_on_measured_images':len(missing),
              'missed_with_half_area_covered_by_sharp':recovered,
              'raw_sharp_objects_matched':sum(r['objects']['matched'] for r in measured),
              'raw_sharp_objects_predicted':sum(r['objects']['predicted'] for r in measured)}
    result = {'state':'completed','created_utc':now(),'counts':counts,'images_sha256':digest(output/'images.json'),
              'protocol_sha256':digest(checked),'test_accessed':False,'calibration_accessed':False,
              'asof_receipt_verified':False,'deployed':False}
    atomic_json(output/'summary.json',result)
    atomic_json(output/'progress.json',{'state':'completed','done':len(rows),'total':len(picked)})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('dataset','baseline','output'):
        parser.add_argument('--'+key,required=True)
    parser.add_argument('--cohorts',nargs='+',required=True)
    parser.add_argument('--days',nargs='+')
    parser.add_argument('--count',type=int,default=24)
    result = run(**vars(parser.parse_args()))
    print(result['counts'],flush=True)


if __name__ == '__main__':
    main()
