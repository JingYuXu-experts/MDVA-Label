"""Check auxiliary CH signals on frozen training references and matched controls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from astropy.io import fits
import numpy as np
from scipy.ndimage import label

from .ch_boundary_growth import CONNECTIVITY
from .datasets import SegmentationDataset
from .durable import atomic_json
from .hmi_angular_projection import local_geometry
from .journal import read
from .runtime import digest, now, output_path


CORE_RADIUS = 200
MATCH_BRIGHTNESS = .10
MATCH_RADIUS = 16


def match_control(y, x, taken, valid, radial, brightness):
    """Translate the same footprint to an unlabeled region with similar 193 and radius."""
    intensity = float(np.median(brightness[y, x]))
    radius = float(radial[y, x].mean())
    choices = []
    for dy in range(-160, 161, 16):
        for dx in range(-160, 161, 16):
            yy, xx = y + dy, x + dx
            if (yy.min() < 0 or xx.min() < 0 or yy.max() >= 512 or xx.max() >= 512
                    or taken[yy, xx].any() or not valid[yy, xx].all()):
                continue
            dr = abs(float(radial[yy, xx].mean()) - radius)
            if dr > MATCH_RADIUS:
                continue
            db = abs(float(np.median(brightness[yy, xx])) / intensity - 1)
            if db <= MATCH_BRIGHTNESS:
                choices.append((db / MATCH_BRIGHTNESS + dr / MATCH_RADIUS, dy, dx, db, dr))
    return min(choices) if choices else None


def features(channels, y, x):
    sampled = {k: v[y, x] for k, v in channels.items()}
    medians = {k: float(np.median(v)) for k, v in sampled.items()}
    hmi = sampled['hmi']
    return {'171_over_193': medians['171'] / medians['193'],
            '211_over_193': medians['211'] / medians['193'],
            'los_sign_imbalance': float(abs(hmi.sum()) / max(np.abs(hmi).sum(), 1e-9))}


def map_channel(folder, record):
    path = (folder / record['filename']).resolve()
    if path.parent != folder.resolve() or digest(path) != record['sha256']:
        raise ValueError('原始通道指纹不符')
    with fits.open(path, memmap=False) as hdus:
        hdu = next(h for h in hdus if h.data is not None and h.data.ndim == 2)
        return local_geometry(hdu.data.astype(np.float64), hdu.header)


def run(dataset, audit, output):
    output = output_path(output, dataset)
    if output.exists() or output.is_relative_to(audit.resolve()):
        raise ValueError('请使用独立且未使用的诊断目录')
    groups = ('train16-01', 'train16-171-01', 'registration16-01', 'hmi-angular16-01')
    indexes = {}
    identities = {}
    for group in groups:
        summary = audit / group / 'summary.json'
        report = read(summary)
        if report['state'] != 'completed' or report['protocol_sha256'] != digest(summary.with_name('protocol.json')):
            raise ValueError(f'{group} 登记身份不符')
        indexes[group] = {row['uri']: row for row in report['results']}
        identities[group] = digest(summary)
    fixed = read(audit / groups[0] / 'protocol.json')
    manifest = digest(dataset / 'manifest-hashes.json')
    if manifest != fixed['manifest_sha256']:
        raise ValueError('训练清单身份不符')
    data = SegmentationDataset(dataset, 'CH', 'train', 512)
    train_rows = {r['uri']: i for i, r in enumerate(data.rows)}
    if any(r['uri'] not in train_rows for r in fixed['rows']):
        raise ValueError('固定样本越过训练范围')
    y_grid, x_grid = np.ogrid[:512, :512]
    radial = np.hypot(y_grid - 255.5, x_grid - 255.5)
    measurements = []
    for row in fixed['rows']:
        uri, short_id = row['uri'], row['id']
        source, add171, geometry, hmi = (indexes[g][uri] for g in groups)
        result = {'uri': uri, 'pairs': [], 'state': 'source_insufficient'}
        measurements.append(result)
        if (not source['alignment_review_gate_passed'] or not add171['source_gate_passed']
                or geometry['source_sha256'] != row['sha256']
                or hmi['state'] != 'angular_sampled_not_physically_certified'):
            continue
        pair = audit / groups[0] / 'pairs' / short_id
        input_audit = read(pair / 'source-audit.json')
        if read(pair / 'mapping-review.json')['source_audit_sha256'] != digest(pair / 'source-audit.json'):
            raise ValueError('原始波段登记已改变')
        records = {r['channel']: r for r in input_audit['sources']}
        if records['193']['sha256'] != hmi['native_aia_sha256']:
            raise ValueError('磁场与EUV的配准身份不符')
        extra = audit / groups[1] / 'pairs' / short_id
        fits171 = next(extra.glob('*.fits'))
        arrays = {k: map_channel(pair, r) for k, r in records.items()}
        arrays['171'] = map_channel(extra, {**add171, 'filename': fits171.name})
        hmi_path = audit / groups[3] / 'pairs' / short_id / 'hmi-on-aia-local512.npy'
        if digest(hmi_path) != hmi['array_sha256']:
            raise ValueError('磁场采样数组身份不符')
        arrays['hmi'] = np.load(hmi_path, allow_pickle=False)
        valid = (radial < CORE_RADIUS) & (arrays['193'] > 0) & np.isfinite(list(arrays.values())).all(axis=0)
        mask = data[train_rows[uri]]['semantic_mask'][0].numpy() > 0
        labels, count = label(mask, structure=CONNECTIVITY)
        result.update(state='measured', references=count, supported=0)
        for region_id in range(1, count + 1):
            y, x = np.where(labels == region_id)
            if len(y) < 32 or not valid[y, x].all():
                continue
            result['supported'] += 1
            control = match_control(y, x, mask, valid, radial, arrays['193'])
            if control is None:
                continue
            _, dy, dx, db, dr = control
            result['pairs'].append({'id': region_id, 'pixels': len(y), 'offset_yx': [dy, dx],
                'brightness_relative_difference': db, 'mean_radius_difference_px': dr,
                'reference': features(arrays, y, x), 'unannotated_control': features(arrays, y + dy, x + dx)})
    statistics = {}
    for feature in ('171_over_193', '211_over_193', 'los_sign_imbalance'):
        pairs = [[p['reference'][feature] - p['unannotated_control'][feature] for p in r['pairs']]
                 for r in measurements if r['pairs']]
        medians = [float(np.median(v)) for v in pairs]
        statistics[feature] = {'median_difference_per_image': float(np.median(medians)) if medians else None,
                               'positive_image_medians': sum(v > 0 for v in medians), 'images': len(medians)}
    report = {'state': 'completed', 'recorded_utc': now(), 'code_sha256': digest(__file__),
              'source_sha256': identities, 'manifest_sha256': manifest, 'fixed_train_images': len(measurements),
              'observations_measured': sum(r['state'] == 'measured' for r in measurements),
              'references_on_measured_images': sum(r.get('references', 0) for r in measurements),
              'references_in_valid_core': sum(r.get('supported', 0) for r in measurements),
              'matched_controls': sum(len(r['pairs']) for r in measurements), 'statistics': statistics,
              'protocol': {'radius_px': CORE_RADIUS, 'brightness_delta_max': MATCH_BRIGHTNESS,
                           'radius_delta_max_px': MATCH_RADIUS, 'control': 'unannotated proxy, not a verified negative',
                           'limits': 'LOS balance on local pixel geometry only; no radial-field flux or instrument calibration claims'},
              'test_accessed': False, 'validation_accessed': False, 'calibration_accessed': False}
    atomic_json(output / 'measurements.json', measurements)
    report['measurements_sha256'] = digest(output / 'measurements.json')
    atomic_json(output / 'summary.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('dataset', 'audit', 'output'):
        parser.add_argument('--' + arg, required=True, type=Path)
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))
