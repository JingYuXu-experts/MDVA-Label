"""Train an AR component precision filter on training observations only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import find_objects, label
from scipy.optimize import linear_sum_assignment
import torch
from torch.nn import functional as F

from .ar_region_retention import filter_by_area
from .ar_scale_review import estimate, native_objects
from .ar_scale_study import ROOT, cohort_rows, configuration, load_plan, warm_model
from .datasets import SegmentationDataset
from .durable import atomic_json
from .forecast_region_detector_audit import MODEL
from .journal import read
from .metrics import binary_segmentation
from .reviewed_train import seed_all
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


FEATURES = ('log_area', 'fill_fraction', 'log_aspect', 'center_distance', 'mean_probability',
            'p90_probability', 'probability_std', 'high_score_fraction')


def describe(values):
    if values.shape != (1024, 1024) or not np.isfinite(values).all():
        raise ValueError('需核验的原生1024网格概率')
    mask = filter_by_area(values >= .4, 256)
    raster, _ = label(mask)
    rows = []
    for identity, slices in enumerate(find_objects(raster), 1):
        sy, sx = slices
        chosen = raster[slices] == identity
        scores = values[slices][chosen]
        width, height = sx.stop - sx.start, sy.stop - sy.start
        rows.append([
            float(np.log1p(len(scores))), len(scores) / (width*height),
            abs(float(np.log(width / height))),
            float(np.hypot((sy.start+sy.stop)/2-512, (sx.start+sx.stop)/2-512)) / 512,
            float(scores.mean()), float(np.quantile(scores, .9)), float(scores.std()),
            float((scores >= .7).mean()),
        ])
    return mask, raster, rows


def match_components(raster, reference, count):
    """Match exactly as native_objects, retaining the component side for fitting."""
    ids = np.unique(reference[reference > 0])
    pred = np.bincount(raster.ravel(), minlength=count+1)[1:]
    areas = np.asarray([(reference == ref).sum() for ref in ids])
    intersection = np.zeros((count, len(ids)), dtype=np.int64)
    for index, ref in enumerate(ids):
        intersection[:, index] = np.bincount(raster[reference == ref], minlength=count+1)[1:]
    union = pred[:, None] + areas[None, :] - intersection
    ratio = np.divide(intersection, union, out=np.zeros_like(union, dtype=float), where=union > 0)
    a, b = linear_sum_assignment(-ratio)
    positives = np.zeros(count, dtype=np.uint8)
    positives[a[ratio[a, b] >= .5]] = 1
    return positives


def fit_features(origin, cohort):
    data = SegmentationDataset(origin['dataset'], 'AR', 'train')
    data.rows = cohort_rows(data, cohort)
    model, _ = warm_model(origin, configuration(origin, 512))
    inputs, labels = [], []
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        seed_all(2026)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        resource = Resources()
        model.cuda().eval()
        with torch.inference_mode():
            for index, row in enumerate(data.rows, 1):
                with Image.open(data.source(row['uri'])) as image:
                    if image.size != (1024, 1024):
                        raise ValueError('只允许已登记HMI网格')
                    gray = image.convert('L').resize((512, 512), Image.Resampling.BILINEAR)
                    gray = np.array(gray, dtype=np.float32) / 255
                tensor = torch.from_numpy(gray)[None, None].cuda()
                with torch.autocast('cuda', dtype=torch.float16):
                    logits = model(tensor)
                p = F.interpolate(logits.float().sigmoid(), size=(1024, 1024), mode='bilinear',
                                  align_corners=False)[0, 0].cpu().numpy()
                _, raster, features = describe(p)
                reference = np.load(data.source(row['mask_uri']), allow_pickle=False)
                inputs.extend(features)
                labels.extend(match_components(raster, reference, len(features)))
                if index % 32 == 0:
                    resource.sample()
        measured = resource.sample()
    return inputs, np.asarray(labels, dtype=int), measured


def run(study, diagnostic, prior, output):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    study, diagnostic, prior = (Path(path).resolve() for path in (study, diagnostic, prior))
    origin = load_plan(study)
    output = output_path(output, origin['dataset'])
    if output.exists() or any(output.is_relative_to(path) for path in (study, diagnostic, prior)):
        raise ValueError('使用新的组件复核目录')
    retention = read(prior / 'summary.json')
    index_path = diagnostic / 'case-index.json'
    if (
        retention['test_accessed'] or retention['index_sha256'] != digest(index_path)
        or retention['images_sha256'] != digest(prior / 'images.json')
        or retention['manifest_sha256'] != origin['data_identity']['manifest-hashes.json']
    ):
        raise ValueError('已登记的0.4/64阈值复算不符')
    val = SegmentationDataset(origin['dataset'], 'AR', 'validation')
    index = {r['uri']: r for r in read(index_path)
             if r['run_id'] == MODEL and r['diagnostic_subset'] == 'validation'}
    old = {r['uri']: r['options']['0.40/64'] for r in read(prior / 'images.json')}
    if set(index) != set(old) or set(index) != {r['uri'] for r in val.rows} or len(val) != 276:
        raise ValueError('开发样本不一致')
    output.mkdir(parents=True)
    protocol = {
        'created_utc': now(), 'code_sha256': digest(__file__), 'source_protocol_sha256': digest(study / 'protocol.json'),
        'retention_sha256': digest(prior / 'summary.json'), 'index_sha256': digest(index_path),
        'features': FEATURES, 'fixed_pixel_score': .4, 'minimum_pixels_native': 256,
        'method': 'balanced standardized logistic C=1, matched reference component mask IoU>=0.5',
        'threshold_policy': 'training-only empirical retention >=99% of correctly matched components; balanced score 0 as contrast',
        'test_accessed': False, 'calibration_accessed': False,
        'limitations': ['Frozen segmentation weights previously saw these training images.',
                        'Unmatched predicted component labels depend on scope of source annotations.'],
    }
    atomic_json(output / 'protocol.json', protocol)
    x, y, resources = fit_features(origin, read(study / 'cohort.json'))
    if len(set(y)) != 2:
        raise ValueError('训练匹配缺少任一类')
    model = make_pipeline(StandardScaler(), LogisticRegression(C=1, class_weight='balanced', random_state=2026))
    model.fit(x, y)
    trained_scores = model.decision_function(x)
    cutoff = float(np.quantile(trained_scores[y == 1], .01, method='lower'))
    arms = {'retain99': cutoff, 'balanced': 0.}
    rows = []
    for image in val.rows:
        uri = image['uri']
        item = index[uri]
        path = diagnostic / 'probabilities' / MODEL / (item['sample_id'] + '.npy')
        if item['mask_uri'] != image['mask_uri'] or item['probability_sha256'] != digest(path):
            raise ValueError('旧概率或参照改变')
        probability = np.load(path, allow_pickle=False)
        ref = np.load(val.source(image['mask_uri']), allow_pickle=False)
        base, raster, vectors = describe(probability)
        measure = native_objects(base, ref)
        actual = (measure['reference'], measure['predicted'], measure['matched'])
        previous = tuple(old[uri][key] for key in ('references', 'predicted', 'matched'))
        if actual != previous:
            raise ValueError(f'旧阈值结果无法复算：{uri}')
        scores = model.decision_function(vectors) if vectors else np.array([])
        masks = {'control': base}
        for arm, threshold in arms.items():
            masks[arm] = np.r_[False, scores >= threshold][raster]
        rows.append({'uri': uri, 'arms': {
            arm: {'pixels': binary_segmentation(mask, ref > 0),
                  'objects': measure if arm == 'control' else native_objects(mask, ref)}
            for arm, mask in masks.items()
        }})
    aggregate = {arm: estimate(rows, arm) for arm in ('control', *arms)}
    changes = {
        arm: {key: aggregate[arm][key] - aggregate['control'][key]
              for key in ('region_precision', 'region_recall', 'false_positive_regions', 'matched_regions')}
        for arm in arms
    }
    scaler, classifier = model.steps[0][1], model.steps[1][1]
    atomic_json(output / 'ranker.json', {
        'features': FEATURES, 'mean': scaler.mean_.tolist(), 'scale': scaler.scale_.tolist(),
        'weights': classifier.coef_[0].tolist(), 'intercept': float(classifier.intercept_[0]),
        'thresholds': arms, 'scores_calibrated': False,
    })
    atomic_json(output / 'images.json', rows)
    report = {
        'state': 'completed', 'created_utc': now(), 'train_components': len(y),
        'train_matched': int(y.sum()), 'validation_images': len(rows), 'results': aggregate, 'deltas': changes,
        'meets_both_80': {arm: value['region_precision'] >= .8 and value['region_recall'] >= .8
                          for arm, value in aggregate.items()},
        'precision_experiment_gate': {arm: values['region_precision'] >= .05 and values['region_recall'] >= -.01
                                      for arm, values in changes.items()},
        'scope': 'Research-only postprocessing on the full development set; no threshold picked from development.',
        'protocol_sha256': digest(output / 'protocol.json'), 'rows_sha256': digest(output / 'images.json'),
        'ranker_sha256': digest(output / 'ranker.json'), 'resources': resources,
        'test_accessed': False, 'calibration_accessed': False, 'released': False,
    }
    atomic_json(output / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('study', 'diagnostic', 'prior', 'output'):
        parser.add_argument('--' + key, required=True)
    args = parser.parse_args()
    configure_environment()
    result = run(**vars(args))
    print(json.dumps({key: result[key] for key in ('deltas', 'precision_experiment_gate')}))


if __name__ == '__main__':
    main()
