"""Verify same-observation HMI LOS FITS against the labeled AR display grid."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import warnings

import numpy as np
from PIL import Image
from scipy.ndimage import map_coordinates
from scipy.optimize import minimize

from mdva.web.historical_hmi import download_hmi_hourly, source_url

from .aia_source_probe import ones_sum
from .ar_scale_study import load_plan
from .datasets import SegmentationDataset
from .durable import atomic_json, commit
from .hmi_source_audit import tai_utc
from .journal import read
from .runtime import digest, now, output_path
from .xrs_coverage import utc


SIZE = 1024
DISPLAY_RADIUS = SIZE / 2.2
ORIENTATIONS = ((1, 1), (1, -1), (-1, 1), (-1, -1))
LIMITS = ((-12, 12), (-12, 12), (.975, 1.025))
ADJUSTMENT_CENTER = np.array([0., 0., 1.])
ADJUSTMENT_RADIUS = np.array([12., 12., .025])
FILENAME = re.compile(r'hmi\.m_720s\.(\d{8}_\d{6})_TAI\.\d+\.magnetogram\.jpg', re.I)


def fits_field(path):
    from astropy.io import fits

    raw = path.read_bytes()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=Warning)
        with fits.open(path, disable_image_compression=True, memmap=False) as hdus:
            integrity = []
            for hdu in hdus:
                if 'CHECKSUM' not in hdu.header:
                    continue
                offsets = hdu.fileinfo()
                section = raw[offsets['hdrLoc']:offsets['datLoc']+offsets['datSpan']]
                integrity.append(ones_sum(section) == 0xffffffff and hdu.verify_datasum() == 1)
        if not integrity or not all(integrity):
            raise ValueError('FITS原始HDU校验失败')
        with fits.open(path, memmap=False) as hdus:
            hdu = next((h for h in hdus if h.data is not None and h.data.ndim == 2), None)
            if hdu is None:
                raise ValueError('FITS没有可读的二维磁图HDU')
            field, header = hdu.data.astype(np.float32), hdu.header.copy()
    if (field.shape != (SIZE, SIZE) or header['BUNIT'] != 'Gauss' or header['CONTENT'] != 'MAGNETOGRAM'
            or header['TELESCOP'] != 'SDO/HMI' or (header['CTYPE1'], header['CTYPE2']) != ('HPLN-TAN', 'HPLT-TAN')
            or (header['CUNIT1'], header['CUNIT2']) != ('arcsec', 'arcsec')):
        raise ValueError('不是已核验规格的SDO/HMI视向高斯磁图')
    return field, header


def source_grid(header):
    x, y = float(header['CRPIX1'])-1, float(header['CRPIX2'])-1
    sx, sy, rsun = (float(header[key]) for key in ('CDELT1', 'CDELT2', 'RSUN_OBS'))
    roll = float(header['CROTA2'])
    if (not np.isfinite([x, y, sx, sy, rsun, roll]).all() or not (450 < x < 575 and 450 < y < 575)
            or not (1.8 < sx < 2.3 and 1.8 < sy < 2.3 and 900 < rsun < 1010 and abs(roll) < .5)
            or any(abs(float(header[key])) > .01 for key in ('CRVAL1', 'CRVAL2'))):
        raise ValueError('源角坐标没有通过几何预检')
    return x, y, rsun/sx, rsun/sy, np.deg2rad(roll)


def sample(field, grid, orientation, adjustment, target_x, target_y):
    x, y, radius_x, radius_y, roll = grid
    dx, dy, scale = adjustment
    tx = (target_x - (SIZE-1)/2 - dx) * orientation[0] / DISPLAY_RADIUS
    ty = (target_y - (SIZE-1)/2 - dy) * orientation[1] / DISPLAY_RADIUS
    rotated_x = tx*np.cos(roll) + ty*np.sin(roll)
    rotated_y = -tx*np.sin(roll) + ty*np.cos(roll)
    coords = np.stack((y + scale*radius_y*rotated_y, x + scale*radius_x*rotated_x))
    return map_coordinates(field, coords, order=1, mode='constant', cval=np.nan, prefilter=False)


def correlation(first, second):
    valid = np.isfinite(first) & np.isfinite(second)
    if valid.sum() < 100 or np.std(first[valid]) < 1e-6 or np.std(second[valid]) < 1e-6:
        return None
    return float(np.corrcoef(first[valid], second[valid])[0, 1])


def register(gray, field, grid):
    if gray.shape != (SIZE, SIZE):
        raise ValueError('本地显示磁图并非已登记1024网格')
    y, x = np.mgrid[48:SIZE-48:5, 48:SIZE-48:5]
    inside = (x-(SIZE-1)/2)**2 + (y-(SIZE-1)/2)**2 < (.79*DISPLAY_RADIUS)**2
    fit = inside & (((y//64 + x//64) % 2) == 0)
    holdout = inside & ~fit
    clipped = np.clip(field, -500, 500)
    choices = []
    for orientation in ORIENTATIONS:
        def objective(values):
            adjustment = ADJUSTMENT_CENTER + values * ADJUSTMENT_RADIUS
            physical = sample(clipped, grid, orientation, adjustment, x[fit], y[fit])
            score = correlation(gray[y[fit], x[fit]], physical)
            return -score if score is not None else 1.

        normalized = minimize(objective, [0., 0., 0.], method='Powell', bounds=((-1, 1),)*3,
                              options={'maxiter': 25, 'xtol': .01}).x
        fit_score = -objective(normalized)
        choices.append((fit_score, orientation, ADJUSTMENT_CENTER + normalized * ADJUSTMENT_RADIUS))
    choices.sort(key=lambda item: item[0], reverse=True)
    fitted, orientation, adjustment = choices[0]
    physical = sample(clipped, grid, orientation, adjustment, x[holdout], y[holdout])
    held = correlation(gray[y[holdout], x[holdout]], physical)
    margin = fitted - choices[1][0]
    away_from_limits = all(low + (high-low)*.02 < value < high - (high-low)*.02
                           for value, (low, high) in zip(adjustment, LIMITS, strict=True))
    accepted = bool(fitted >= .75 and held is not None and held >= .75 and margin >= .15 and away_from_limits)
    return {'accepted': accepted, 'fit_correlation': fitted, 'heldout_correlation': held, 'orientation_margin': margin,
            'away_from_parameter_limits': away_from_limits, 'orientation': list(orientation),
            'adjustment_xy_scale': adjustment.tolist(), 'fit_pixels': int(fit.sum()), 'heldout_pixels': int(holdout.sum())}


def observations(data, study, prior):
    train, validation = (SegmentationDataset(data, 'AR', subset) for subset in ('train', 'validation'))
    cohort = read(study / 'cohort.json')
    rng = np.random.default_rng(2026)
    selected = []
    for band in ('under256', '256to1023', '1024plus'):
        rows = sorted((row['uri'] for row in cohort if row['trial_stratum'] == band))
        selected.extend(sorted(rng.choice(rows, 6, replace=False).tolist()))
    cases = {row['uri'] for row in read(prior)['by_region']}
    comparison = sorted(row['uri'] for row in validation.rows if row['uri'] not in cases)
    cases.update(rng.choice(comparison, 12, replace=False).tolist())
    result = [(split, row) for split, source, ids in (('train', train, set(selected)), ('validation', validation, cases))
              for row in source.rows if row['uri'] in ids]
    if len(result) != len(selected) + len(cases):
        raise ValueError('HMI核验名单与冻结数据不同')
    return result, {'train': train, 'validation': validation}


def snapshot(row, folder, cached=None):
    match = FILENAME.fullmatch(Path(row['uri']).name)
    if not match:
        raise ValueError('本地命名没有明确HMI记录时刻')
    nominal = match[1]
    url, filename = source_url(nominal)
    folder.mkdir(parents=True, exist_ok=True)
    path, metadata_path = folder / filename, folder / 'source.json'
    meta = read(metadata_path)
    if meta:
        if (meta['url'], meta['nominal_tai'], meta['sha256']) != (url, nominal, digest(path)):
            raise ValueError('官方缓存身份不符')
    else:
        previous = read(cached / 'source.json') if cached is not None else None
        if previous:
            archived = cached / filename
            if (previous['url'], previous['nominal_tai'], previous['sha256']) != (url, nominal, digest(archived)):
                raise ValueError('之前的原始FITS缓存身份不符')
            raw, meta = archived.read_bytes(), previous
        else:
            raw, meta = download_hmi_hourly(nominal)
        commit(path, lambda stream: stream.write(raw))
        if not previous:
            meta.update(sha256=digest(path), fetched_utc=now(), bytes=len(raw))
        atomic_json(metadata_path, meta)
    return path, meta


def review(source, row, split, folder, cached):
    result = {'uri': row['uri'], 'split': split}
    try:
        path, meta = snapshot(row, folder, cached)
        field, header = fits_field(path)
        recorded, observed = (tai_utc(header[key]) for key in ('T_REC', 'T_OBS'))
        observation = utc(row['time_utc'])
        delta = (observed - observation).total_seconds()
        result.update(source=meta, quality=header['QUALITY'], observed_utc=observed.isoformat(),
                      archive_creation=header.get('DATE'), delta_seconds=delta, units='Gauss', field='B_LOS')
        if recorded != observation or abs(delta) > 60:
            return {**result, 'state': 'source_time_mismatch'}
        if header['QUALITY'] != 0:
            return {**result, 'state': 'quality_review_required'}
        grid = source_grid(header)
        with Image.open(source.source(row['uri'])) as image:
            image = np.array(image.convert('L'))
        registration = register(image, field, grid)
        result['registration'] = registration
        if not registration['accepted']:
            return {**result, 'state': 'registration_not_supported'}
        y, x = np.indices(image.shape, dtype=np.float64)
        aligned = sample(field, grid, registration['orientation'], registration['adjustment_xy_scale'], x, y)
        path = folder / 'aligned-los.npy'
        commit(path, lambda stream: np.save(stream, aligned, allow_pickle=False))
        return {**result, 'state': 'display_alignment_supported', 'array_sha256': digest(path),
                'finite_fraction': float(np.isfinite(aligned).mean())}
    except (OSError, ValueError, KeyError) as exc:
        return {**result, 'state': 'unavailable', 'error': f'{type(exc).__name__}: {exc}'}


def run(study, prior, output, cached=None):
    study, prior = Path(study).resolve(), Path(prior).resolve()
    plan = load_plan(study)
    output = output_path(output, plan['dataset'])
    if output.exists() or output.is_relative_to(study) or output.is_relative_to(prior.parent):
        raise ValueError('磁图对照需独立新目录')
    tiny = read(prior)
    if tiny['state'] != 'completed' or tiny['test_accessed']:
        raise ValueError('极小区域队列未冻结')
    rows, datasets = observations(plan['dataset'], study, prior)
    if cached is not None:
        cached = Path(cached).resolve()
        previous = read(cached / 'summary.json')
        cached_protocol = cached / 'protocol.json'
        if (previous['state'] != 'completed' or previous['protocol_sha256'] != digest(cached_protocol)
                or read(cached_protocol)['prior_sha256'] != digest(prior) or output.is_relative_to(cached)):
            raise ValueError('旧磁图缓存必须属于同一个队列及独立目录')
        cached_by_uri = {record['uri']: cached / 'pairs' / record['id'] for record in previous['results']}
    else:
        cached_by_uri = {}
    protocol = {'created_utc': now(), 'code_sha256': digest(__file__), 'study_sha256': digest(study / 'protocol.json'),
                'prior_sha256': digest(prior), 'data': plan['data_identity'],
                'cached_report_sha256': digest(cached / 'summary.json') if cached is not None else None,
                'selection': [{'uri': row['uri'], 'split': split, 'id': str(i)} for i, (split, row) in enumerate(rows)],
                'scope': 'Localized display alignment of processed 1024 LOS archive. No masks used for registration.',
                'orientation_selection': 'Fit checkerboard of 64px blocks; score other blocks, without identity labels.',
                'training_allowed': False, 'test_accessed': False, 'calibration_accessed': False}
    output.mkdir(parents=True)
    atomic_json(output / 'protocol.json', protocol)

    def worker(item):
        index, (split, row) = item
        folder = output / 'pairs' / str(index)
        result = review(datasets[split], row, split, folder, cached_by_uri.get(row['uri']))
        atomic_json(folder / 'review.json', result)
        return {'id': str(index), **result}

    records = []
    # Raw and decompressed FITS inspection use distinct shared Astropy settings.
    for item in enumerate(rows):
        row = worker(item)
        records.append(row)
        if len(records) % 4 == 0:
            print(json.dumps({'reviewed': len(records), 'total': len(rows), 'latest': row['state']}), flush=True)
    result = {'state': 'completed', 'results': records, 'counts': dict(Counter(row['state'] for row in records)),
              'protocol_sha256': digest(output / 'protocol.json'), 'units': 'Gauss', 'training_allowed': False,
              'physical_angular_wcs_certified_for_jpeg': False, 'operational_availability_verified': False,
              'test_accessed': False, 'calibration_accessed': False}
    atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('study', 'prior', 'output'):
        parser.add_argument('--' + key, required=True)
    parser.add_argument('--cached')
    report = run(**vars(parser.parse_args()))
    print(json.dumps(report['counts']))


if __name__ == '__main__':
    main()
