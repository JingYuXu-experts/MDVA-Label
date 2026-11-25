"""Compare historical disk-wide flare models with and without region features."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import label
import torch
from torch.nn import functional as F

from . import reviewed_train as engine
from .ar_scale_study import ROOT, configuration, load_plan, warm_model
from .datasets import SegmentationDataset
from .durable import atomic_json
from .journal import read
from .metrics import forecast_scores
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path
from .xrs_coverage import utc


Y, X = np.ogrid[:1024, :1024]
CORE = (Y - 511.5)**2 + (X - 511.5)**2 < 400**2
RAW = ('contrast_fraction32', 'abs_contrast_q95', 'abs_contrast_q99', 'abs_contrast_mean')
REGIONS = ('region_fraction', 'region_count', 'largest_fraction', 'largest_share')
ARMS = {'raw': RAW, 'regions': REGIONS, 'raw_plus_regions': (*RAW, *REGIONS)}


def cohort(audit, origin):
    report = read(audit / 'summary.json')
    path = audit / 'windows.json'
    if (
        report['state'] != 'completed' or report['test_accessed'] or report['calibration_accessed']
        or report['windows_sha256'] != digest(path)
        or report['frozen_manifest_sha256'] != origin['data_identity']['manifest-hashes.json']
    ):
        raise ValueError('必须从相同冻结数据和全日面核验窗口准备')
    datasets = {split: SegmentationDataset(origin['dataset'], 'AR', split) for split in ('train', 'validation')}
    indexed = {split: {row['uri']: row for row in data.rows} for split, data in datasets.items()}
    rows, excluded, identities = [], Counter(), {}
    for window in read(path):
        split = window['split']
        if split not in datasets:
            raise ValueError('预报试点只允许开发划分')
        if window['target'] is None or window['split_boundary']:
            excluded['unknown_or_boundary'] += 1
            continue
        if len(window['images']) != 1:
            excluded['ambiguous_observation'] += 1
            continue
        uri = window['images'][0]['uri']
        source = indexed[split].get(uri)
        if source is None or utc(source['time_utc']) != utc(window['observed_utc']):
            raise ValueError('真实推理观察时刻和标签窗口不符')
        fingerprint = datasets[split].fingerprints[uri]['sha256']
        if fingerprint in identities:
            raise ValueError('图像指纹重复，需单独审核时间分区')
        identities[fingerprint] = uri
        rows.append({'uri': uri, 'split': split, 'observed_utc': window['observed_utc'],
                     'target_end_utc': window['target_end_utc'], 'target': window['target'],
                     'source_sha256': fingerprint})
    if not rows or max(utc(row['target_end_utc']) for row in rows if row['split'] == 'train') >= min(
        utc(row['observed_utc']) for row in rows if row['split'] == 'validation'
    ):
        raise ValueError('训练目标窗口与开发观察时刻相交')
    return rows, datasets, dict(excluded)


def features(gray, prediction):
    contrast = np.abs(gray[CORE].astype(np.float32) - np.median(gray[CORE]))
    labeled, count = label(prediction)
    area = int(prediction.sum())
    maximum = int(np.bincount(labeled.ravel())[1:].max(initial=0))
    return {
        'contrast_fraction32': float((contrast >= 32).mean()),
        'abs_contrast_q95': float(np.percentile(contrast, 95)) / 255,
        'abs_contrast_q99': float(np.percentile(contrast, 99)) / 255,
        'abs_contrast_mean': float(contrast.mean()) / 255,
        'region_fraction': area / prediction.size, 'region_count': count,
        'largest_fraction': maximum / prediction.size, 'largest_share': maximum / area if area else 0.,
    }


def readout(model, image, native):
    array = np.array(image.resize((512, 512), Image.Resampling.BILINEAR), dtype=np.float32) / 255.
    tensor = torch.from_numpy(array)[None, None].cuda()
    with torch.autocast('cuda', dtype=torch.float16):
        logits = model(tensor)
    probability = F.interpolate(logits.float().sigmoid(), size=native.shape,
                                mode='bilinear', align_corners=False)[0, 0].cpu().numpy()
    if not np.isfinite(probability).all():
        raise ValueError('固定检测器输出非有限值')
    return features(native, probability >= .5)


def model_comparison(rows):
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import average_precision_score
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    train = [row for row in rows if row['split'] == 'train']
    validation = [row for row in rows if row['split'] == 'validation']
    fit_targets = np.array([row['target'] for row in train])
    targets = np.array([row['target'] for row in validation])
    prevalence = float(fit_targets.mean())
    scores = {'training_prevalence': np.full(len(validation), prevalence)}
    weights = {}
    for name, keys in ARMS.items():
        x_train = [[row['features'][key] for key in keys] for row in train]
        x_valid = [[row['features'][key] for key in keys] for row in validation]
        model = make_pipeline(StandardScaler(), LogisticRegression(C=1., max_iter=500, random_state=2026))
        model.fit(x_train, fit_targets)
        fitted = model.steps[1][1]
        scaler = model.steps[0][1]
        scores[name] = model.predict_proba(x_valid)[:, 1]
        weights[name] = {'features': list(keys), 'coef': fitted.coef_[0].tolist(),
                         'intercept': float(fitted.intercept_[0]), 'mean': scaler.mean_.tolist(),
                         'scale': scaler.scale_.tolist(), 'iterations': int(fitted.n_iter_[0])}
    reports = {name: forecast_scores(targets, values, climate=prevalence) for name, values in scores.items()}
    groups = np.array([int(datetime.fromisoformat(row['observed_utc']).timestamp() // (86400*27))
                       for row in validation])
    blocks = [np.flatnonzero(groups == key) for key in np.unique(groups)]
    rng = np.random.default_rng(2026)
    deltas_ap, deltas_brier = [], []
    for _ in range(2000):
        selected = np.concatenate([blocks[index] for index in rng.integers(len(blocks), size=len(blocks))])
        y = targets[selected]
        if np.unique(y).size != 2:
            continue
        a, b = (scores[key][selected] for key in ('raw', 'raw_plus_regions'))
        deltas_ap.append(average_precision_score(y, b) - average_precision_score(y, a))
        deltas_brier.append(float(np.mean((b-y)**2 - (a-y)**2)))
    comparisons = {
        'paired_region_increment_ap_ci95': np.quantile(deltas_ap, [.025, .975]).tolist(),
        'paired_region_increment_brier_ci95': np.quantile(deltas_brier, [.025, .975]).tolist(),
        'bootstrap_blocks': len(blocks), 'valid_replicates': len(deltas_ap),
        'development_positives': int(targets.sum()), 'train_positives': int(fit_targets.sum()),
    }
    predictions = [{'uri': row['uri'], 'target': row['target'],
                    'outputs': {name: float(values[index]) for name, values in scores.items()}}
                   for index, row in enumerate(validation)]
    return reports, comparisons, weights, predictions


def run(source, audit, output):
    source, audit = Path(source).resolve(), Path(audit).resolve()
    origin = load_plan(source)
    output = output_path(output, origin['dataset'])
    if output.exists() or any(output.is_relative_to(path) for path in (source, audit)):
        raise ValueError('预测对照请使用独立输出目录')
    rows, datasets, excluded = cohort(audit, origin)
    model, _ = warm_model(origin, configuration(origin, 512))
    plan = {
        'created_utc': now(), 'source_sha256': digest(source / 'protocol.json'),
        'label_audit_sha256': digest(audit / 'summary.json'), 'model_sha256': origin['weights_sha256'],
        'code_sha256': digest(__file__), 'arms': ARMS,
        'scope': 'Historical train and temporally later development, fixed logistic models C=1, no selection.',
        'limitations': [
            'Detector weights saw segmentation labels for training-time images. Area descriptors are not out-of-fold.',
            'Actual acquisition latency and occurrence of definitive revisions at forecast time are not verified.',
            'No independent test, calibration, or prospective reliability conclusion.',
        ],
        'test_accessed': False, 'calibration_accessed': False,
    }
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        engine.seed_all(2026)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        resource = Resources()
        resource.sample()
        model.cuda().eval()
        output.mkdir(parents=True)
        atomic_json(output / 'protocol.json', plan)
        with torch.inference_mode():
            for index, row in enumerate(rows, 1):
                data = datasets[row['split']]
                with Image.open(data.source(row['uri'])) as image:
                    if image.size != (1024, 1024):
                        raise ValueError('只允许已登记的完整HMI显示磁图')
                    gray = image.convert('L')
                    row['features'] = readout(model, gray, np.array(gray))
                if index % 64 == 0 or index == len(rows):
                    atomic_json(output / 'progress.json', {'state': 'extracting', 'images': index, 'total': len(rows),
                                                           'resource': resource.sample()})
        resources = resource.sample()
    atomic_json(output / 'features.json', rows)
    reports, comparisons, weights, predictions = model_comparison(rows)
    atomic_json(output / 'research-models.json', weights)
    atomic_json(output / 'development-predictions.json', predictions)
    report = {
        'state': 'completed', 'created_utc': now(), 'observations': len(rows), 'excluded': excluded,
        'by_split': dict(Counter(row['split'] for row in rows)), 'metrics': reports, 'comparison': comparisons,
        'resources': resources, 'scope': plan['scope'], 'limitations': plan['limitations'],
        'protocol_sha256': digest(output / 'protocol.json'), 'features_sha256': digest(output / 'features.json'),
        'dev_predictions_sha256': digest(output / 'development-predictions.json'),
        'research_models_sha256': digest(output / 'research-models.json'),
        'test_accessed': False, 'calibration_accessed': False, 'operational': False,
    }
    atomic_json(output / 'summary.json', report)
    atomic_json(output / 'progress.json', {'state': 'completed', 'images': len(rows), 'total': len(rows)})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'audit', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    configure_environment()
    report = run(**vars(args))
    print(json.dumps({key: report[key] for key in ('by_split', 'metrics', 'comparison')}))


if __name__ == '__main__':
    main()
