"""Audit HMI display-pair geometry without inferring missing FITS coordinates."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter

from .datasets import SegmentationDataset
from .durable import atomic_json
from .journal import read
from .runtime import configure_environment, digest, now, output_path


ANGLE = np.linspace(0, 2 * np.pi, 360, endpoint=False)
RADIUS = np.arange(448, 497, dtype=float)
RAY_X = np.rint(511.5 + np.cos(ANGLE)[:, None] * RADIUS).astype(int)
RAY_Y = np.rint(511.5 + np.sin(ANGLE)[:, None] * RADIUS).astype(int)
FIT = np.stack((np.ones_like(ANGLE), np.cos(ANGLE), np.sin(ANGLE)), axis=1)


def read_native(path):
    with Image.open(path) as image:
        if image.size != (1024, 1024):
            raise ValueError('只接受已登记的1024像素JPG')
        return np.array(image.convert('L'))


def frame(image):
    """Fit the white display border. This is separate from the physical limb."""
    white = image[RAY_Y, RAY_X] >= 232
    stable = np.logical_and.accumulate(white[:, ::-1], axis=1)[:, ::-1]
    eligible = stable[:, -1] & ~stable[:, 0]
    if eligible.sum() < 320:
        return None
    edge = RADIUS[np.argmax(stable, axis=1)]
    mask = eligible.copy()
    for _ in range(3):
        fit, *_ = np.linalg.lstsq(FIT[mask], edge[mask], rcond=None)
        mask = eligible & (np.abs(edge - FIT @ fit) <= 2)
        if mask.sum() < 300:
            return None
    return fit.tolist()


def texture_maps(magnetic, continuum):
    """Extract display-domain structure for a registration check, preserving orientation."""
    y, x = np.ogrid[:512, :512]
    core = (x - 255.5)**2 + (y - 255.5)**2 < 200**2

    def shrink(image):
        return np.asarray(Image.fromarray(image).resize((512, 512), Image.Resampling.BILINEAR), dtype=np.float32)

    mag, aux = shrink(magnetic), shrink(continuum)
    strong = gaussian_filter(np.maximum(np.abs(mag - np.median(mag[core])) - 20, 0), 2)
    dark = gaussian_filter(np.maximum(gaussian_filter(aux, 8) - aux, 0), 2)
    return strong, dark, core


def inspect_pair(magnetic, continuum):
    first, second = frame(magnetic), frame(continuum)
    if first is None or second is None:
        return {'state': 'display_frame_uncertain'}
    delta = np.asarray(second) - first
    center = float(np.hypot(*delta[1:]))
    if center > 2.5 or abs(delta[0]) > 2:
        return {'state': 'display_frame_disagrees', 'center_delta': center, 'radius_delta': float(delta[0])}
    strong, dark, core = texture_maps(magnetic, continuum)
    anchor = strong[core]

    def correlate(candidate):
        texture = candidate[core]
        if min(float(anchor.std()), float(texture.std())) < 1e-6:
            return 0.
        return float(np.corrcoef(anchor, texture)[0, 1])

    original = correlate(dark)
    alternative = max(correlate(np.rot90(array, rotation))
                      for array in (dark, dark[:, ::-1]) for rotation in range(4)
                      if array is not dark or rotation)
    if original >= .15 and original > alternative + .05:
        translated = [(correlate(np.roll(dark, (dy, dx), axis=(0, 1))), dx, dy)
                      for dx in (-3, 0, 3) for dy in (-3, 0, 3)]
        best, dx, dy = max(translated)
        if best - original > .05:
            state = 'texture_shift_uncertain'
        else:
            state = 'supported_display_pair'
    else:
        best, dx, dy = None, None, None
        state = 'texture_orientation_unconfirmed'
    return {'state': state, 'center_delta': center, 'radius_delta': float(delta[0]),
            'original_texture_correlation': original, 'alternative_correlation': alternative,
            'translated_correlation': best, 'translation_probe_px512': [dx, dy]}


def run(dataset, source, output):
    dataset, source = Path(dataset).resolve(), Path(source).resolve()
    output = output_path(output, dataset)
    if output.exists() or output.is_relative_to(source):
        raise ValueError('配准研究使用新的输出目录')
    training = read(source / 'summary.json')
    validation = read(source / 'validation-availability-summary.json')
    if (
        training['observations_sha256'] != digest(source / 'cohort.json')
        or validation['rows_sha256'] != digest(source / 'validation-availability.json')
        or validation['source_audit_sha256'] != digest(source / 'summary.json')
    ):
        raise ValueError('配对清单指纹不符')
    datasets = {split: SegmentationDataset(dataset, 'AR', split) for split in ('train', 'validation')}
    sources = {
        'train': [(row['uri'], row['continuum_uri']) for row in read(source / 'cohort.json')],
        'validation': [(row['image_uri'], row['continuum_uri']) for row in read(source / 'validation-availability.json')],
    }
    output.mkdir(parents=True)
    rows = []
    for split, pairs in sources.items():
        data = datasets[split]
        allowed = {row['uri'] for row in data.rows}
        for uri, auxiliary in pairs:
            if uri not in allowed or not auxiliary.startswith('@solar_obs/Magnetograms and Continuum/Continuum/'):
                raise ValueError('图像不在允许的已配对开发样本')
            evidence = inspect_pair(read_native(data.source(uri)), read_native(data.source(auxiliary)))
            rows.append({'split': split, 'uri': uri, 'aux_uri': auxiliary, **evidence})
            if len(rows) % 40 == 0:
                atomic_json(output / 'progress.json', {'checked': len(rows), 'total': sum(map(len, sources.values()))})
    atomic_json(output / 'pairs.json', rows)
    report = {
        'state': 'completed', 'created_utc': now(), 'pairs_sha256': digest(output / 'pairs.json'),
        'source_sha256': digest(source / 'summary.json'), 'code_sha256': digest(__file__),
        'manifest_sha256': digest(dataset / 'manifest-hashes.json'),
        'by_split': {split: dict(Counter(row['state'] for row in rows if row['split'] == split)) for split in sources},
        'scope': 'Empirical display-border and structures; no shifts or flips applied. Weak and conflicting pairs abstain.',
        'physical_wcs_verified': False, 'test_accessed': False,
    }
    atomic_json(output / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset', 'source', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    configure_environment()
    print(json.dumps(run(**vars(args))['by_split']))


if __name__ == '__main__':
    main()
