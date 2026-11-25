"""Display-domain CH audit; training-only anchor, no physical flux calibration."""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path

import numpy as np
from PIL import Image

from .datasets import SegmentationDataset
from .diagnostic_support import sample_id
from .durable import atomic_json, commit
from .runtime import digest, now, output_path
from .spatial_audit import apparent_disk


def measure(gray):
    """No mask, prediction or sample identity enters the input transform."""
    gray = np.asarray(gray)
    if gray.ndim != 2 or gray.dtype != np.uint8:
        raise ValueError('仅接受已核验的8位灰度显示图')
    disk = apparent_disk(gray)
    y, x = np.ogrid[:gray.shape[0], :gray.shape[1]]
    rho = np.hypot(x-disk['cx'], y-disk['cy'])/disk['radius']
    core = gray[rho < .8]
    median = float(np.median(core))
    if median <= 0 or not len(core):
        raise ValueError('日面内部灰度无法标准化')
    return {'disk': disk, 'core_median': median,
            'core_quantiles_10_50_90': np.quantile(core, [.1, .5, .9]).tolist()}


def normalize(gray, anchor):
    """One preregistered scalar correction; unsupported images stay unchanged."""
    gray = np.asarray(gray)
    if not np.isfinite(anchor) or not 0 < anchor < 255:
        raise ValueError('训练锚点非法')
    try:
        info = measure(gray)
    except ValueError as exc:
        return gray.astype(np.float32)/255., {'applied': False, 'reason': str(exc)}
    factor = anchor/info['core_median']
    if not .5 <= factor <= 2:
        return gray.astype(np.float32)/255., {**info, 'applied': False,
            'reason': '所需倍率不在预登记0.5—2范围内', 'factor': factor}
    d = info['disk']
    y, x = np.ogrid[:gray.shape[0], :gray.shape[1]]
    support = np.hypot(x-d['cx'], y-d['cy']) <= d['radius']
    result = gray.astype(np.float32)
    result[support] = np.clip(result[support]*factor, 0, 255)
    return result/255., {**info, 'applied': True, 'factor': factor,
                         'clipped_pixels': int(np.sum(gray[support]*factor > 255))}


def fit_anchor(rows):
    if any(r['split'] != 'train' for r in rows):
        raise ValueError('亮度锚点只能从训练图像拟合')
    values = [r['core_median'] for r in rows if r.get('available')]
    if not values:
        raise ValueError('没有可核验训练日面')
    return float(np.median(values))


def summarize(rows):
    valid = [r for r in rows if r['available']]
    return {'images': len(rows), 'available': len(valid), 'unavailable': len(rows)-len(valid),
            'core_median': float(np.median([r['core_median'] for r in valid])) if valid else None,
            'core_median_quantiles_10_50_90': np.quantile([r['core_median'] for r in valid],[.1,.5,.9]).tolist() if valid else None}


def prepare(dataset, output):
    dataset = Path(dataset).resolve()
    output = output_path(output, dataset)
    if output.exists():
        raise ValueError('输入审计版本已存在，禁止覆盖')
    output.mkdir(parents=True)
    rows = []
    for split in ('train', 'validation'):
        data = SegmentationDataset(dataset, 'CH', split)
        for index, row in enumerate(data.rows):
            image = data.source(row['uri'])
            with Image.open(image) as im: gray = np.asarray(im.convert('L'))
            result = {'uri': row['uri'], 'split': split, 'year': row['time_utc'][:4],
                      'sample_id': sample_id(row['uri']), 'image_sha256': digest(image)}
            try: result.update(available=True, **measure(gray))
            except ValueError as exc: result.update(available=False, reason=str(exc))
            rows.append(result)
            if index % 100 == 0:
                atomic_json(output/'status.json', {'state': 'auditing_inputs', 'split': split,
                    'completed_in_split': index+1, 'total_in_split': len(data), 'updated_utc': now(),
                    'GPU_used': False, 'model_training': False})
    anchor = fit_anchor([r for r in rows if r['split'] == 'train'])
    raw = ''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in rows).encode('utf-8')
    commit(output/'images.jsonl', lambda stream: stream.write(raw))
    report = {'state': 'inputs_audited_probe_not_run', 'created_utc': now(), 'category': 'CH',
        'anchor': anchor, 'anchor_fit_split': 'train', 'core_radius_fraction': .8,
        'transform': 'multiply visible disk by training anchor / current core median; clip to 0..255; preserve outside',
        'fallback': 'unchanged original input when fit unavailable or factor outside [0.5,2]',
        'threshold': .5, 'weights_changed': False, 'masks_used_to_fit_transform': False,
        'physical_calibration': False, 'test_or_calibration_images_opened': False,
        'splits': {s: summarize([r for r in rows if r['split'] == s]) for s in ('train','validation')},
        'by_year': {year: summarize([r for r in rows if r['year']==year]) for year in sorted({r['year'] for r in rows})},
        'frozen_manifest_sha256': digest(dataset/'manifest-hashes.json'),
        'input_rows_sha256': digest(output/'images.jsonl'),
        'code': {'ch_display_audit.py': digest(__file__), 'spatial_audit.py': digest(Path(__file__).with_name('spatial_audit.py'))},
        'hypothesis': 'display brightness distribution shift may contribute to central-region missed detections; correlation is not causation',
        'plan': 'one frozen-weight validation-only transform; no training, no parameter search, no deployment replacement',
        'next_gate': 'half-region coverage +3pp; zero-overlap count -5%; Dice loss <=1pp; Precision loss <=3pp; preserve whole validation with fallback'}
    atomic_json(output/'protocol.json', report)
    atomic_json(output/'status.json', {'state': 'inputs_audited_probe_not_run','updated_utc':now()})
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', required=True);parser.add_argument('--output', required=True)
    args = parser.parse_args()
    from .runtime import configure_environment
    configure_environment()
    print(json.dumps(prepare(args.dataset,args.output),ensure_ascii=False,indent=2))
