"""Fit H-alpha display corrections on training images and replay a frozen checkpoint."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import find_objects, label
import torch

from .datasets import SegmentationDataset
from .diagnostic_support import connected_scores
from .durable import atomic_json
from .fl_author_model import AuthorGraph
from .fl_ensemble_study import CONNECTIVITY, DATASET, PILOT, ROOT
from .fl_pilot_review import region_summary
from .journal import read
from .metrics import binary_segmentation, segmentation_summary
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path

AUDIT = PILOT.parent / 'fl-training-display-domain-01'
STATIONS = ('BBSO_Halpha', 'HSOS_Halpha', 'KSO_Halpha')
RADII = np.linspace(.05, .95, 10)
ARMS = ('raw', 'radial', 'radial_and_scale')


def disk_radius_map(gray):
    if gray.shape != (512, 512) or not np.isfinite(gray).all():
        return None
    regions, n = label(gray > 32 / 255)
    if not n:
        return None
    sizes = np.bincount(regions.ravel())[1:]
    index = int(sizes.argmax())
    y, x = find_objects(regions)[index]
    h, w = y.stop - y.start, x.stop - x.start
    if not (.58 < sizes[index] / gray.size < .73 and 440 <= min(h, w) <= max(h, w) <= 490
            and abs(w - h) <= 16 and min(y.start, x.start, 512 - y.stop, 512 - x.stop) >= 10):
        return None
    yy, xx = np.ogrid[:512, :512]
    return np.hypot((yy - (y.start + y.stop - 1) / 2) / (h / 2),
                    (xx - (x.start + x.stop - 1) / 2) / (w / 2))


def disk_profile(gray, radius):
    medians = []
    for r in RADII:
        ring = gray[(radius >= r - .05) & (radius < r + .05)]
        medians.append(float(np.median(ring)))
    central = gray[radius <= .7]
    q10, median, q90 = np.quantile(central, [.1, .5, .9])
    return {'radial': medians, 'median': float(median), 'contrast': float(q90 - q10)}


def fit_station_profiles():
    audited_rows = AUDIT / 'rows.json'
    audit = read(AUDIT / 'summary.json')
    if audit['rows_sha256'] != digest(audited_rows) or audit['test_accessed']:
        raise ValueError('训练台站审计的来源身份改变')
    training = SegmentationDataset(DATASET, 'FL', 'train', 512)
    by_uri = {row['uri']: row for row in training.rows}
    selected = read(audited_rows)
    if len(selected) != 96 or len({row['uri'] for row in selected}) != 96:
        raise ValueError('台站校正需要原固定的96张训练图')
    groups = defaultdict(list)
    for row in selected:
        uri = row['uri']
        station = row['instrument']
        if uri not in by_uri or by_uri[uri]['instrument'] != station:
            raise ValueError('台站样本身份与训练清单不符')
        with Image.open(training.source(uri)) as im:
            gray = np.asarray(im.convert('L'), dtype=np.float32) / 255
        radius = disk_radius_map(gray)
        if radius is not None:
            groups[station].append(disk_profile(gray, radius))
    if set(groups) != set(STATIONS) or min(map(len, groups.values())) < 24:
        raise ValueError('台站中通过日面几何质检的训练图不足')
    return {
        station: {'valid_training_images': len(values),
                  **{key: np.median([v[key] for v in values], axis=0).tolist()
                     for key in ('radial', 'median', 'contrast')}}
        for station, values in groups.items()
    }


def corrected(gray, radius, source, target, scale=False):
    source_radial = np.asarray(source['radial']) - source['median']
    target_radial = np.asarray(target['radial']) - target['median']
    if scale:
        factor = np.clip(target['contrast'] / max(source['contrast'], .025), .7, 1.4)
        background = np.interp(radius, RADII, source['radial'])
        destination = np.interp(radius, RADII, target['radial'])
        difference = (gray - background) * factor + destination - gray
    else:
        difference = np.interp(radius, RADII, target_radial - source_radial)
    edge_weight = np.clip((.99 - radius) / .11, 0, 1)
    return np.clip(gray + edge_weight * difference, 0, 1).astype(np.float32)


def summarize(rows):
    states = {}
    for arm in ARMS:
        cases = [row['arms'][arm] for row in rows]
        states[arm] = {'pixels': segmentation_summary([c['pixels'] for c in cases]),
                       'regions': region_summary([c['regions'] for c in cases])}
    return states


def run(output):
    output = output_path(output, DATASET)
    if output.exists():
        raise ValueError('台站校正使用新的诊断目录')
    source = PILOT / 'checkpoint.pt'
    report = read(PILOT / 'summary.json')
    if (report['state'] != 'completed' or report['test_accessed'] or report['calibration_accessed']
            or report['checkpoint_sha256'] != digest(source)):
        raise ValueError('台站校正的权重身份不符')
    saved = load_checkpoint(source)
    if saved['epoch'] != report['epoch']:
        raise ValueError('台站校正的检查点轮次不符')
    profiles = fit_station_profiles()
    model = AuthorGraph('batch')
    model.load_state_dict(saved['model'])
    dev = SegmentationDataset(DATASET, 'FL', 'validation', 512)
    if len(dev) != 182 or {r['instrument'] for r in dev.rows} != set(STATIONS):
        raise ValueError('暗条开发集或台站身份不符')
    protocol = {
        'split': 'validation', 'fit_split': 'train', 'source_sha256': digest(__file__),
        'train_receipt_sha256': digest(AUDIT / 'rows.json'), 'checkpoint_sha256': digest(source),
        'mask_threshold': .5, 'station_reference': STATIONS[0], 'profiles': profiles, 'radii': RADII.tolist(),
        'test_accessed': False, 'calibration_accessed': False,
    }
    atomic_json(output / 'protocol.json', protocol)
    rows = []
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cuda.matmul.allow_tf32 = False
        model.cuda().eval()
        resources = Resources()
        with torch.inference_mode():
            for i, row in enumerate(dev.rows):
                gray = dev[i]['image'][0].numpy()
                ref = np.load(dev.source(row['mask_uri']), allow_pickle=False) > 0
                components = label(ref, structure=CONNECTIVITY)[0]
                radius = disk_radius_map(gray)
                station = row['instrument']
                candidates = {'raw': gray, 'radial': gray, 'radial_and_scale': gray}
                if radius is not None and station != STATIONS[0]:
                    target = profiles[STATIONS[0]]
                    candidates['radial'] = corrected(gray, radius, profiles[station], target)
                    candidates['radial_and_scale'] = corrected(gray, radius, profiles[station], target, scale=True)
                scores = {}
                for name, pixels in candidates.items():
                    if name != 'raw' and pixels is gray:
                        scores[name] = scores['raw']
                        continue
                    probability = model(torch.from_numpy(pixels)[None, None].cuda()).float().sigmoid()[0, 0].cpu().numpy()
                    if not np.isfinite(probability).all():
                        raise ValueError('暗条台站校正后输出非有限值')
                    mask = probability >= .5
                    scores[name] = {'pixels': binary_segmentation(mask, ref), 'regions': connected_scores(mask, components)}
                rows.append({'uri': row['uri'], 'station': station, 'time_utc': row['time_utc'],
                             'geometry_accepted': radius is not None, 'arms': scores})
                if i % 24 == 0:
                    resources.sample()
        use = resources.sample()
    combined = summarize(rows)
    if abs(combined['raw']['pixels']['dice'] - report['metrics']['dice']) > 1e-5:
        raise ValueError('原图精度无法复算，拒绝台站对照')
    station_rows = {s: [r for r in rows if r['station'] == s] for s in STATIONS}
    stations = {s: summarize(cases) for s, cases in station_rows.items()}
    control = combined['raw']['regions']
    gate = {}
    for name in ARMS[1:]:
        candidate = combined[name]['regions']
        gate[name] = (candidate['recall'] >= control['recall'] + .03
                      and candidate['precision'] >= control['precision'] - .01
                      and all(m[name]['pixels']['dice'] >= m['raw']['pixels']['dice'] - .03
                              for m in stations.values()))
    atomic_json(output / 'images.json', rows)
    result = {
        'state': 'completed', 'created_utc': now(), 'images': len(rows), 'gate_passed': gate,
        'all': combined, 'stations': stations,
        'geometry_accepted': {s: sum(row['geometry_accepted'] for row in cases) for s, cases in station_rows.items()},
        'images_sha256': digest(output / 'images.json'), 'protocol_sha256': digest(output / 'protocol.json'),
        'resources': use, 'test_accessed': False, 'calibration_accessed': False, 'deployed': False,
    }
    atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    configure_environment()
    report = run(parser.parse_args().output)
    print(json.dumps({key: report[key] for key in ('state', 'images', 'gate_passed', 'all')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
