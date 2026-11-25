"""Measure LOS magnetic structure near independently corroborated GONG filaments."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import numpy as np
from astropy.io import fits
from PIL import Image
from scipy.ndimage import distance_transform_edt, gaussian_filter

from .ar_hmi_registration import fits_field
from .durable import atomic_json, commit
from .fl_candidate_review_2021 import publicly_paired
from .fl_gong_hmi_geometry import NAME, SITES, reduced, weak_pil
from .fl_magfilo_audit import DATA_SHA256, raster
from .fl_magfilo_transfer import LABEL_SHA256
from .fl_reference_consensus_audit import reference_parts
from .hmi_angular_projection import angular_sample, wcs
from .hmi_nrt_pair_audit import path_identity
from .hmi_source_audit import tai_utc
from .journal import read
from .runtime import digest, now, output_path


ALPHA_ARCHIVE = 'https://gong2.nso.edu/HA/haf/'
ALPHA_ARCHIVE_MIRROR = 'https://gong2.nso.edu/ftp/HA/haf/'
SIZE = 512
MAX_TIME_OFFSET_SECONDS = 1800
NEAR_PIL_PIXELS = 3


def legacy_wcs(header):
    if (header.get('CTYPE1'), header.get('CTYPE2')) != ('GONGX', 'GONGY'):
        raise ValueError('旧 Hα 坐标类型不符')
    radius, x, y, major, minor, angle = (float(header[k]) for k in
        ('SOLAR-R', 'FNDLMBXC', 'FNDLMBYC', 'FNDLMBMA', 'FNDLMBMI', 'FNDLMBAN'))
    if (not np.isfinite([radius, x, y, major, minor, angle]).all()
            or not 900 < radius < 1010 or (x, y, major, minor, angle) != (1024, 1024, 900, 900, 0)):
        raise ValueError('旧 Hα 文件不是已登记的重映射日面')
    scale = radius / major
    return {'CTYPE1': 'HPLN-TAN', 'CTYPE2': 'HPLT-TAN', 'CUNIT1': 'arcsec', 'CUNIT2': 'arcsec',
            'CRPIX1': x + .5, 'CRPIX2': y + .5, 'CRVAL1': 0., 'CRVAL2': 0.,
            'CDELT1': scale, 'CDELT2': scale, 'CROTA1': 0., 'CROTA2': 0.}


def load_alpha(path):
    identity = NAME.fullmatch(path.name)
    if identity is None:
        raise ValueError('科研 Hα 文件名不符')
    with fits.open(path, disable_image_compression=True, memmap=False) as hdus:
        if not any(h.header.get('ZIMAGE') is True for h in hdus) or any(
                h.verify_checksum() != 1 or h.verify_datasum() != 1 for h in hdus):
            raise ValueError('科研 Hα 压缩 FITS 校验失败')
    with fits.open(path, memmap=False) as hdus:
        img = next(h for h in hdus if h.data is not None and h.data.ndim == 2)
        header = img.header.copy()
        # This legacy product explicitly labels DATE-OBS as UTC in its card comment.
        stamp = datetime.fromisoformat(header['DATE-OBS']).replace(tzinfo=timezone.utc)
        if (img.data.shape != (2048, 2048) or header['TELESCOP'] != 'NSO-GONG'
                or header['IMTYPE'] != 'H-ALPHA' or not 6562.6 <= header['WAVELNTH'] <= 6563.
                or 'UTC' not in header.comments['DATE-OBS'] or header['SITE'] != SITES[identity[2]]
                or stamp.strftime('%Y%m%d%H%M%S') != identity[1]):
            raise ValueError('科研 Hα 文件尺寸、站点或时间不符')
        intensity = img.data.astype(np.float32).reshape(SIZE, 4, SIZE, 4).mean(axis=(1, 3))
    return intensity, header, stamp


def display_alignment(alpha, jpeg):
    """Check published JPEG orientation against texture in the native FITS."""
    def band(image):
        return gaussian_filter(image, .8) - gaussian_filter(image, 4.)

    y, x = np.indices(alpha.shape)
    central = np.hypot(y - 255.5, x - 255.5) <= 150
    detail, display = band(alpha), band(jpeg)[central]
    scores = {}
    for key, plane in (('same', detail), ('both_flipped', detail[::-1, ::-1]),
                       ('vertical_flip', detail[::-1]), ('horizontal_flip', detail[:, ::-1])):
        values = plane[central]
        scores[key] = float(np.corrcoef(values, display)[0, 1]) if values.std() and display.std() else None
    return scores


def corroboration_signals(distance, valid, references, tags):
    controls = [(np.rot90(distance, k), np.rot90(valid, k)) for k in (1, 2, 3)]
    mirrored, mirror_valid = distance[:, ::-1], valid[:, ::-1]
    result = []
    for variant, mask in enumerate(references):
        regions, sizes = reference_parts(mask)
        confirmed = [t for t in tags if t['variant'] == variant and t['basis'] == 'corroborated_at_iou_50']
        for tag in confirmed:
            identity = tag['reference_id']
            region = regions == identity
            if sizes[identity - 1]['area'] != tag['area']:
                raise ValueError('源标注与一致区域身份不符')
            if not valid[region].all() or not mirror_valid[region].all() or any(
                    not support[region].all() for _, support in controls):
                continue
            fraction = float(np.mean(distance[region] <= NEAR_PIL_PIXELS))
            rotated = [float(np.mean(control[region] <= NEAR_PIL_PIXELS)) for control, _ in controls]
            result.append({**tag, 'fraction_within_3_pixels': fraction, 'rotated_controls': rotated,
                           'east_west_mirror_control': float(np.mean(mirrored[region] <= NEAR_PIL_PIXELS)),
                           'observed_minus_control': fraction - float(np.mean(rotated))})
    return result


def compare(row, reference, photo, verified, captures, receipts, annotations, output):
    name = row['file']
    stem = Path(name).stem
    alpha_name = stem + '.fits.fz'
    alpha_url = f'{ALPHA_ARCHIVE}{stem[:6]}/{stem[:8]}/{alpha_name}'
    timestamp = datetime.fromisoformat(row['captured_utc']).replace(tzinfo=timezone.utc)
    # For 2021, UTC +37 s is TAI. The file header independently supplies the actual observation time.
    nominal = (timestamp + timedelta(seconds=37, minutes=30)).replace(minute=0, second=0, microsecond=0)
    hmi_name, hmi_url = path_identity('definitive', nominal.strftime('%Y%m%d_%H%M%S'))
    for item, urls in ((alpha_name, (alpha_url, alpha_url.replace(ALPHA_ARCHIVE, ALPHA_ARCHIVE_MIRROR, 1))),
                       (hmi_name, (hmi_url,))):
        entry = receipts.get(item, {})
        if not entry.get('sha256'):
            return {'image': name, 'state': 'capture_missing', 'file': item}
        if entry['url'] not in urls or entry['sha256'] != digest(captures / item):
            raise ValueError('源地址或下载指纹不符')
    file = verified / 'jpeg' / name
    if digest(file) != photo['image_sha256']:
        raise ValueError('人工标注图指纹不符')
    with Image.open(file) as image:
        if image.format != 'JPEG' or image.size != (2048, 2048):
            raise ValueError('人工标注图不是原 2048 JPEG')
        gray = np.asarray(image.convert('L').resize((SIZE, SIZE), Image.Resampling.BOX), dtype=np.float32)
    alpha, h, stamp = load_alpha(captures / alpha_name)
    field, m = fits_field(captures / hmi_name)
    delta = (stamp - tai_utc(m['T_OBS'])).total_seconds()
    if timestamp != stamp or tai_utc(m['T_REC']) != nominal - timedelta(seconds=37):
        raise ValueError('科研 Hα 或 HMI 名义观测时刻不符')
    geometry = reduced(legacy_wcs(h))
    scores = display_alignment(alpha, gray)
    flags = {k: int(h[k]) for k in ('HAONBAND', 'HAERRCOD', 'ECLIPSE')}
    info = {'image': name, 'station': h['SITE'], 'alpha_file': alpha_name, 'hmi_file': hmi_name,
            'signed_time_offset_seconds': delta, 'flags': flags, 'hmi_quality': int(m['QUALITY']),
            'alignment_correlations': scores}
    expected = scores['vertical_flip']
    competitors = max(v if v is not None else 1. for k, v in scores.items() if k != 'vertical_flip')
    if expected is None or expected < .7 or expected - competitors < .25:
        return {**info, 'state': 'display_alignment_unverified'}
    if abs(delta) > MAX_TIME_OFFSET_SECONDS or m['QUALITY'] != 0 or flags != {'HAONBAND': 1, 'HAERRCOD': 0, 'ECLIPSE': 0}:
        return {**info, 'state': 'observing_conditions_outside_protocol'}
    if abs(float(m['RSUN_OBS']) - float(h['SOLAR-R'])) > 2 or abs(float(m['CRLT_OBS']) - float(h['SOLAR-B0'])) > .05:
        return {**info, 'state': 'apparent_solar_geometry_mismatch'}
    aligned, sampling = angular_sample(field, m, geometry, alpha.shape)
    y, x = np.indices(alpha.shape)
    lon, lat = wcs(geometry).all_pix2world(x, y, 0)
    central = np.hypot(((lon + 180) % 360 - 180) * 3600, lat * 3600) <= float(h['SOLAR-R']) * .75
    valid = central & np.isfinite(aligned)
    pil = weak_pil(aligned, valid)[::-1]
    distance = distance_transform_edt(~pil) if pil.any() else np.full(pil.shape, np.inf)
    masks = [raster(annotations[v['id']].values()) for v in row['annotators']]
    records = corroboration_signals(distance, valid[::-1], masks, reference['references'])
    file = output / 'pairs' / (stem + '.npz')
    commit(file, lambda f: np.savez_compressed(
        f, los_gauss=np.asarray(aligned[::-1], dtype=np.float32), valid=valid[::-1], los_sign_transition=pil))
    return {**info, 'state': 'angular_comparison_available', 'los_transition_pixels': int(pil.sum()),
            'central_support_fraction': float(valid.sum() / central.sum()), 'sampling': sampling,
            'asset': file.relative_to(output).as_posix(), 'asset_sha256': digest(file), 'references': records}


def aggregate(rows):
    groups = {}
    for name, cohort in (('all_30min', rows), ('within_15min', [r for r in rows if abs(r['signed_time_offset_seconds']) <= 900])):
        refs = [(r['image'], x) for r in cohort for x in r['references']]
        for mode, selected in (('all_corroborated', refs),
                               ('complete_misses', [(i, x) for i, x in refs if x['best_original_candidate_iou'] == 0]),
                               ('strict_matches', [(i, x) for i, x in refs if x['review_matched']])):
            groups[f'{name}/{mode}'] = {'records': len(selected), 'dates': len({i[:8] for i, _ in selected}),
                'mean_within_3_pixels': float(np.mean([x['fraction_within_3_pixels'] for _, x in selected])) if selected else None,
                'mean_rotated_control': float(np.mean([x['rotated_controls'] for _, x in selected])) if selected else None,
                'mean_east_west_mirror': float(np.mean([x['east_west_mirror_control'] for _, x in selected])) if selected else None,
                'records_with_positive_enrichment': sum(x['observed_minus_control'] > 0 for _, x in selected)}
    return groups


def run(dataset, verified, agreement, captures, source, release, output):
    output = output_path(output, dataset)
    if output.exists() or any(output.is_relative_to(p.resolve()) for p in (verified, agreement, captures)):
        raise ValueError('暗条历史配对诊断使用独立新目录')
    previous, consensus = (read(p / 'summary.json') for p in (verified, agreement))
    receipt = read(captures / 'receipt.json')
    selection = read(captures / 'selection.json')
    if (any(s['state'] != 'completed' or s['test_accessed'] or s['calibration_accessed'] for s in (previous, consensus))
            or consensus['prior_sha256'] != digest(verified / 'summary.json')
            or consensus['images_sha256'] != digest(agreement / 'images.json')
            or selection['source_sha256'] != digest(agreement / 'summary.json')
            or receipt['selection_sha256'] != digest(captures / 'selection.json')
            or digest(source) != DATA_SHA256 or digest(release) != LABEL_SHA256):
        raise ValueError('人工参考、诊断结果或源观测清单身份不符')
    observations, annotations = publicly_paired(source, release)
    photos = {r['file']: r for r in read(verified / 'inventory.json')}
    refs = {r['image']: r for r in read(agreement / 'images.json') if r['variants'] >= 2}
    if set(refs) != {r['image'] for r in selection['observations']} or set(refs) - photos.keys():
        raise ValueError('须按清单检查所有多人工标注图')
    entries = {r['file']: r for r in receipt['files']}
    if len(entries) != len(receipt['files']) or set(entries) != set(selection['files']):
        raise ValueError('下载收据重复或不完整')
    cases = []
    for row in observations:
        if (name := row['file']) not in refs:
            continue
        try:
            info = compare(row, refs[name], photos[name], verified, captures, entries, annotations, output)
        except (ValueError, KeyError, OSError) as exc:
            info = {'image': name, 'state': 'source_validation_failed', 'reason': str(exc)}
        cases.append(info)
    passed = [r for r in cases if r['state'] == 'angular_comparison_available']
    atomic_json(output / 'cases.json', cases)
    summary = {'state': 'completed', 'recorded_utc': now(), 'code_sha256': digest(__file__),
               'source_receipt_sha256': digest(captures / 'receipt.json'),
               'cases_sha256': digest(output / 'cases.json'), 'counts': dict(Counter(r['state'] for r in cases)),
               'coverage': aggregate(passed), 'test_accessed': False, 'calibration_accessed': False,
               'trained': False, 'prediction_issued': False,
               'scope': 'Previously viewed 2021 diagnostic data, approximate angular reprojection, LOS sign transition; no duplicate-variant adjustment and no inference of FL identity, field topology or eruptivity',
               'limitations': 'No differential rotation, observer displacement or filament-height correction. Rotated controls preserve radius, not local magnetic activity and are not physical negative examples.'}
    atomic_json(output / 'summary.json', summary)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('dataset', 'verified', 'agreement', 'captures', 'source', 'release', 'output'):
        parser.add_argument('--' + argument, type=Path, required=True)
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))
