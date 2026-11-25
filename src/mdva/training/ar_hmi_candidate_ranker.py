"""Compare JPG and calibrated LOS candidate ranking without changing production masks."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import nullcontext
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import find_objects, label
import torch
from torch.nn import functional as F

from .ar_component_verifier import match_components
from .ar_hmi_region_audit import measure
from .ar_region_retention import filter_by_area
from .ar_scale_review import native_objects
from .ar_scale_study import ROOT, configuration, load_plan, warm_model
from .datasets import SegmentationDataset
from .durable import atomic_json
from .forecast_region_detector_audit import MODEL
from .journal import read
from .reviewed_train import seed_all
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path
from .xrs_coverage import utc


DISPLAY = ('log_area', 'fill_fraction', 'mean_score', 'jpeg_absolute_p95_gray_levels')
PHYSICAL = (*DISPLAY, 'absolute_p95_gauss', 'minimum_polarity_fraction')
RETAIN = .97
LANES = ('development', 'small_region_stress')


def block(time):
    return int(utc(time).timestamp() // (27*86400))


def policy_split(training):
    groups = sorted({block(row['time_utc']) for row in training})
    if len(groups) < 10:
        raise ValueError('训练时段不够分离拟合与阈值选择')
    boundary = groups[int(.75*len(groups))]
    fitting = [row for row in training if block(row['time_utc']) < boundary-1]
    policy = [row for row in training if block(row['time_utc']) >= boundary]
    return fitting, policy


def candidates(probability, reference, field, gray):
    if probability.shape != reference.shape or not np.isfinite(probability).all():
        raise ValueError('候选和参考未对齐或存在非有限值')
    mask = filter_by_area(probability >= .4, 64)
    raster, count = label(mask)
    matches = match_components(raster, reference, count)
    rows = []
    for component, window in enumerate(find_objects(raster), 1):
        pixels = raster[window] == component
        area = int(pixels.sum())
        feature = measure(field, gray, window, pixels) if field is not None else {'state': 'physical_unavailable'}
        observed = {'component': component, 'area': area, 'matched_reference': bool(matches[component-1]),
                    'overlap_reference': bool(np.any(reference[window][pixels] > 0)), **feature}
        if observed['state'] == 'measured':
            observed.update(log_area=float(np.log1p(area)), fill_fraction=area / int(pixels.size),
                            mean_score=float(probability[window][pixels].mean()))
        rows.append(observed)
    return mask, raster, rows


def fit(data):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    fitting, policy = policy_split(data)
    truth = lambda rows: np.asarray([row['matched_reference'] for row in rows])
    y, hold = truth(fitting), truth(policy)
    if min(int(y.sum()), int((~y).sum())) < 15 or int(hold.sum()) < 10:
        raise ValueError('拟合或阈值组没有足够的真候选，以前次模型替代会混用协议')
    models = {}
    for name, fields in (('display', DISPLAY), ('physical', PHYSICAL)):
        estimator = make_pipeline(StandardScaler(), LogisticRegression(C=1, class_weight='balanced', random_state=2026))
        estimator.fit([[item[key] for key in fields] for item in fitting], y)
        scores = estimator.decision_function([[item[key] for key in fields] for item in policy])
        keep_at = float(np.quantile(scores[hold], 1-RETAIN, method='lower'))
        scale, logit = estimator.steps[0][1], estimator.steps[1][1]
        models[name] = {'features': fields, 'mean': scale.mean_.tolist(), 'scale': scale.scale_.tolist(),
                        'weights': logit.coef_[0].tolist(), 'intercept': float(logit.intercept_[0]),
                        'review_threshold': keep_at, 'policy_recall': float((scores[hold] >= keep_at).mean())}
    return models, {'fitting_candidates': len(y), 'fitting_matched': int(y.sum()), 'threshold_candidates': len(hold),
                    'threshold_matched': int(hold.sum()), 'fitting_images': len({row['uri'] for row in fitting}),
                    'threshold_images': len({row['uri'] for row in policy})}


def decision(row, model):
    if row['state'] != 'measured':
        return None, True
    values = np.asarray([row[name] for name in model['features']])
    score = float(((values - model['mean']) / model['scale']) @ model['weights'] + model['intercept'])
    return score, score >= model['review_threshold']


def infer(model, gray):
    image = Image.fromarray(gray).resize((512, 512), Image.Resampling.BILINEAR)
    x = torch.from_numpy(np.array(image, dtype=np.float32) / 255)[None, None].cuda()
    with torch.inference_mode(), torch.autocast('cuda', dtype=torch.float16):
        logits = model(x)
    return F.interpolate(logits.float().sigmoid(), size=gray.shape, mode='bilinear', align_corners=False)[0, 0].cpu().numpy()


def tally(rows, name):
    objects = [row['objects'][name] for row in rows]
    counts = {key: sum(obj[key] for obj in objects) for key in ('reference', 'predicted', 'matched')}
    counts['unmatched_candidates'] = counts['predicted'] - counts['matched']
    counts['region_precision'] = counts['matched'] / max(1, counts['predicted'])
    counts['region_recall'] = counts['matched'] / max(1, counts['reference'])
    for size in ('under256', '256to1023'):
        group = [obj['by_size'].get(size, {}) for obj in objects]
        counts[size] = {key: sum(obj.get(key, 0) for obj in group) for key in ('reference', 'matched')}
    return counts


def metrics(images, area_limit=None):
    from sklearn.metrics import average_precision_score

    results = {name: tally(images, name) for name in ('baseline', 'display', 'physical')}
    supported = [row for image in images for row in image['candidates']
                 if row['state'] == 'measured' and (area_limit is None or row['area'] < area_limit)]
    truth = [r['matched_reference'] for r in supported]
    ap = {name: float(average_precision_score(truth, [r['scores'][name] for r in supported]))
          for name in ('display', 'physical')} if len(set(truth)) == 2 else {}
    return {'images': len(images), 'supported_candidates': len(supported), 'candidate_match_ap': ap, 'results': results}


def run(study, cohort, diagnostic, retention, output, small_only=False):
    study, cohort, diagnostic, retention = (Path(p).resolve() for p in (study, cohort, diagnostic, retention))
    origin = load_plan(study)
    output = output_path(output, origin['dataset'])
    if output.exists() or any(output.is_relative_to(path) for path in (study, cohort, diagnostic, retention)):
        raise ValueError('研究复核使用新目录')
    completed = read(cohort / 'summary.json')
    old = read(retention / 'summary.json')
    index_path = diagnostic / 'case-index.json'
    protocol = read(cohort / 'protocol.json')
    if (completed['state'] != 'completed' or completed['test_accessed'] or completed['calibration_accessed']
            or completed['protocol_sha256'] != digest(cohort / 'protocol.json') or old['test_accessed']
            or protocol['data'] != origin['data_identity'] or old['index_sha256'] != digest(index_path)
            or old['images_sha256'] != digest(retention / 'images.json')):
        raise ValueError('已冻结输入或旧评价缓存身份不符')
    expected = {r['uri']: r['options']['0.40/16'] for r in read(retention / 'images.json')}
    index = {r['uri']: r for r in read(index_path) if r['run_id'] == MODEL and r['diagnostic_subset'] == 'validation'}
    datasets = {split: SegmentationDataset(origin['dataset'], 'AR', split) for split in ('train', 'validation')}
    sources = {split: {r['uri']: r for r in ds.rows} for split, ds in datasets.items()}
    fitting = []
    evaluated = []
    area_limit = 512 if small_only else None
    output.mkdir(parents=True)
    selection = {
        'cohort_summary_sha256': digest(cohort / 'summary.json'), 'retention_sha256': digest(retention / 'summary.json'),
        'code_sha256': digest(__file__), 'retention_goal_training': RETAIN, 'pixel_threshold': .4, 'minimum_area': 64,
        'candidate_area_limit': area_limit,
        'gate': {'region_precision_gain': .03, 'region_recall_max_loss': .01, 'physical_ap_gain_over_display': .02},
        'unknown_policy': 'Keep original candidate if physical input or local alignment is unavailable.',
        'scope': 'Historical only. Segmentation weights already saw the training images. Candidate masks are research-only.',
        'test_accessed': False, 'calibration_accessed': False,
    }
    atomic_json(output / 'protocol.json', selection)
    config = configuration(origin, 512)
    model, _ = warm_model(origin, config)
    resource = None
    for training in (True, False):
        rows = [r for r in completed['results'] if (r['lane'] == 'training') == training]
        with exclusive(ROOT / '.mdva/training-gpu.lock') if training else nullcontext():
            if training:
                seed_all(2026)
                torch.backends.cuda.matmul.allow_tf32 = False
                torch.backends.cudnn.allow_tf32 = False
                torch.cuda.set_per_process_memory_fraction(.75)
                torch.cuda.reset_peak_memory_stats()
                usage = Resources()
                model.cuda().eval()
            for number, registered in enumerate(rows, 1):
                uri, split = registered['uri'], registered['split']
                data, row = datasets[split], sources[split][uri]
                field_path = cohort / 'pairs' / registered['id'] / 'aligned-los.npy'
                field = None
                if registered['state'] == 'display_alignment_supported':
                    if digest(field_path) != registered['array_sha256']:
                        raise ValueError('配对磁图发生变化')
                    field = np.load(field_path, allow_pickle=False)
                elif training:
                    continue
                with Image.open(data.source(uri)) as im:
                    gray = np.array(im.convert('L'))
                reference = np.load(data.source(row['mask_uri']), allow_pickle=False)
                if training:
                    probability = infer(model, gray)
                else:
                    case = index[uri]
                    path = diagnostic / 'probabilities' / MODEL / (case['sample_id'] + '.npy')
                    if case['mask_uri'] != row['mask_uri'] or digest(path) != case['probability_sha256']:
                        raise ValueError('旧概率或参考身份不符')
                    probability = np.load(path, allow_pickle=False)
                base, raster, regions = candidates(probability, reference, field, gray)
                if training:
                    fitting.extend({**r, 'uri': uri, 'time_utc': row['time_utc']} for r in regions
                                   if r['state'] == 'measured' and (area_limit is None or r['area'] < area_limit))
                    if number % 16 == 0:
                        usage.sample()
                    continue
                base_metrics = native_objects(base, reference)
                keys = ('reference', 'predicted', 'matched')
                if tuple(base_metrics[key] for key in keys) != tuple(expected[uri][key] for key in ('references', 'predicted', 'matched')):
                    raise ValueError('验证候选旧结果未复现')
                geometry = {'baseline': base_metrics}
                for name, parameters in models.items():
                    kept = [False]
                    for region in regions:
                        if area_limit is not None and region['area'] >= area_limit:
                            score, keep = None, True
                        else:
                            score, keep = decision(region, parameters)
                        region.setdefault('scores', {})[name] = score
                        kept.append(keep)
                    geometry[name] = native_objects(np.asarray(kept)[raster], reference)
                evaluated.append({'uri': uri, 'lane': registered['lane'], 'time_utc': row['time_utc'],
                                  'source_state': registered['state'], 'objects': geometry, 'candidates': regions})
            if training:
                resource = usage.sample()
        if training:
            models, fitted_counts = fit(fitting)
            model.cpu()
    results = {lane: metrics([r for r in evaluated if r['lane'] == lane], area_limit) for lane in LANES}
    main = results['development']
    base, physical = main['results']['baseline'], main['results']['physical']
    delta = {key: physical[key] - base[key] for key in ('region_precision', 'region_recall', 'unmatched_candidates')}
    ap = main['candidate_match_ap']
    stress = results['small_region_stress']['results']
    tiny_preserved = stress['physical']['under256']['matched'] >= stress['baseline']['under256']['matched']
    gate = (delta['region_precision'] >= .03 and delta['region_recall'] >= -.01 and bool(ap)
            and (not small_only or tiny_preserved)
            and ap['physical'] - ap['display'] >= .02)
    atomic_json(output / 'training-candidates.json', fitting)
    atomic_json(output / 'images.json', evaluated)
    atomic_json(output / 'rankers.json', models)
    report = {'state': 'completed', 'created_utc': now(), 'fitting': fitted_counts, 'results': results,
              'development_physical_change': delta, 'pilot_gate_passed': gate, 'small_only': small_only,
              'tiny_stress_matched_count_retained': tiny_preserved,
              'source_states': dict(Counter(r['source_state'] for r in evaluated)), 'resource': resource,
              'test_accessed': False, 'calibration_accessed': False, 'deployed': False,
              'images_sha256': digest(output / 'images.json'), 'rankers_sha256': digest(output / 'rankers.json'),
              'training_candidates_sha256': digest(output / 'training-candidates.json'),
              'protocol_sha256': digest(output / 'protocol.json')}
    atomic_json(output / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('study', 'cohort', 'diagnostic', 'retention', 'output'):
        parser.add_argument('--' + key, required=True)
    parser.add_argument('--small-only', action='store_true')
    configure_environment()
    result = run(**vars(parser.parse_args()))
    print(json.dumps({key: result[key] for key in ('fitting', 'results', 'development_physical_change', 'pilot_gate_passed')}))


if __name__ == '__main__':
    main()
