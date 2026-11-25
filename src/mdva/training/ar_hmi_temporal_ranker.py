"""Train-period replay of a previous-day HMI verifier on fixed AR candidates."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import find_objects, label
from scipy.optimize import linear_sum_assignment
import torch
from torch.nn import functional as F

from .ar_continuum_study import expand
from .ar_hmi_candidate_neighbors import (
    carrington_drift, checked_frame, compare_fields, inverse_disk, nominal, projected_disk,
)
from .ar_hmi_candidate_ranker import PHYSICAL, block, candidates, decision, tally
from .ar_hmi_registration import sample
from .ar_hmi_segmentation import HmiDataset, setup
from .ar_native_tiles import add_isolated
from .ar_scale_review import native_objects
from .ar_scale_study import ROOT, warm_model
from .ar_srs_geometry_audit import MINIMUM_COS_ANGLE
from .durable import atomic_json
from .journal import read
from .reviewed_train import load_checkpoint, seed_all
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


SPATIAL = (*PHYSICAL, 'supplement')
TEMPORAL = (*SPATIAL, 'signed_correlation', 'previous_log_field_ratio')
RETENTION = .98
TIME_WINDOW_SECONDS = 120


def time_partition(rows):
    blocks = sorted({block(row['time_utc']) for row in rows})
    if len(blocks) < 16:
        raise ValueError('时间块不足，不能拟合和独立核验')
    cut1, cut2 = blocks[len(blocks)//2], blocks[3*len(blocks)//4]
    return {
        'fit': [row for row in rows if block(row['time_utc']) < cut1-1],
        'policy': [row for row in rows if cut1 <= block(row['time_utc']) < cut2-1],
        'held': [row for row in rows if block(row['time_utc']) >= cut2],
    }


def object_scores(raster, parts, reference, use):
    reference_ids, sizes = np.unique(reference[reference > 0], return_counts=True)
    iou = np.zeros((len(parts), len(sizes)), dtype=np.float64)
    for column, (rid, area) in enumerate(zip(reference_ids, sizes, strict=True)):
        hits = np.bincount(raster[reference == rid], minlength=len(parts)+1)[1:]
        union = np.asarray([item['area'] for item in parts]) + area - hits
        iou[:, column] = hits / union
    result = {'reference_ids': reference_ids.tolist(), 'reference_sizes': sizes.tolist(), 'iou': iou.tolist()}
    return build_metrics(result, use)


def matching(geometry, chosen):
    overlap = np.asarray(geometry['iou'], dtype=float).reshape(len(chosen), len(geometry['reference_ids']))
    selected = np.flatnonzero(chosen)
    a, b = linear_sum_assignment(-overlap[selected])
    valid = overlap[selected[a], b] >= .5
    return selected[a[valid]], b[valid]


def build_metrics(geometry, chosen):
    selected, columns = matching(geometry, chosen)
    found = set(columns.tolist())
    ids, sizes = geometry['reference_ids'], geometry['reference_sizes']
    groups = defaultdict(Counter)
    for index, size in enumerate(sizes):
        group = 'under256' if size < 256 else ('256to1023' if size < 1024 else '1024plus')
        groups[group].update({'reference': 1, 'matched': int(index in found)})
    return ({
        'reference': len(ids), 'predicted': int(np.count_nonzero(chosen)), 'matched': len(found),
        'by_size': {name: dict(counts) for name, counts in groups.items()},
        'matched_reference_ids': [rid for index, rid in enumerate(ids) if index in found],
    }, geometry, set(selected.tolist()))


def prior_field(folder, acquired, expected, original_observed):
    if acquired['previous_tai'] != nominal(expected):
        raise ValueError('邻帧缓存与候选时刻不符')
    if acquired['state'] != 'downloaded':
        return None, 'archive_unavailable'
    receipt = acquired['receipt']
    try:
        frame = checked_frame(folder / nominal(expected) / receipt['filename'], receipt, expected)
    except (OSError, ValueError, KeyError):
        return None, 'quality_or_geometry_rejected'
    delta = (frame[3] - original_observed).total_seconds()
    if abs(delta + 86400) > TIME_WINDOW_SECONDS:
        return None, 'time_rejected'
    return frame, 'measured'


def temporal_fields(current, header, registration, observed, previous, window):
    y, x = np.mgrid[window[0].start:window[0].stop, window[1].start:window[1].stop]
    lat, lon, mu = inverse_disk(x, y, header, registration)
    field, prev_header, grid, prev_observed = previous
    moved = lon + carrington_drift(lat, (prev_observed-observed).total_seconds())
    px, py, prev_mu = projected_disk(lat, moved, prev_header, registration)
    visible = np.isfinite(mu) & (mu >= MINIMUM_COS_ANGLE) & (prev_mu >= MINIMUM_COS_ANGLE)
    earlier = sample(field, grid, registration['orientation'], registration['adjustment_xy_scale'], px, py)
    measurements = compare_fields(current[window], earlier, visible)
    correlation = measurements.get('signed_correlation')
    if correlation is None:
        return {}
    ratio = (measurements['neighbor_p95_gauss']+1) / (measurements['current_p95_gauss']+1)
    return {'signed_correlation': correlation, 'previous_log_field_ratio': float(np.log(ratio))}


def network_candidates(model, inputs, reference, field, gray):
    with torch.autocast('cuda', dtype=torch.float16):
        logits = model(inputs)
    probabilities = F.interpolate(logits.float().sigmoid(), size=reference.shape, mode='bilinear',
                                  align_corners=False)[0, 0].cpu().numpy()
    return candidates(probabilities, reference, field, gray)


def collect_item(data, index, models, params, threshold, intake, cache):
    row = data.rows[index]
    registered = data.pairs[row['uri']]
    item = data[index]
    field, header, observed, earlier = None, None, None, None
    state = 'current_physical_unavailable'
    if item['physical_used']:
        folder = data.cohort / 'pairs' / registered['id']
        field = np.load(folder/'aligned-los.npy', allow_pickle=False)
        original = registered['source']
        stamp = datetime.strptime(original['nominal_tai'], '%Y%m%d_%H%M%S')
        _, header, _, observed = checked_frame(folder/original['filename'], original, stamp)
        earlier, state = prior_field(cache, intake[original['nominal_tai']], stamp-timedelta(days=1), observed)
    with Image.open(data.source(row['uri'])) as im:
        gray = np.array(im.convert('L'))
    reference = np.load(data.source(row['mask_uri']), allow_pickle=False)
    input_pair = item['image'][None].cuda()
    _, starting, regions = network_candidates(models['starting'], input_pair[:, :1], reference, field, gray)
    keep = [False, *(r['area'] >= 512 or decision(r, params)[1] for r in regions)]
    starting_mask = np.asarray(keep)[starting]
    _, extra, supplements = network_candidates(models['hmi'], input_pair, reference, field, gray)
    keep = [False]
    for region in supplements:
        score, _ = decision(region, params)
        keep.append(region['area'] < 512 and score is not None and score >= threshold)
    fused = add_isolated(starting_mask, np.asarray(keep)[extra])
    raster, count = label(fused)
    parts = []
    for component, window in enumerate(find_objects(raster), 1):
        pixels = raster[window] == component
        supplemental = not starting_mask[window][pixels].any()
        original = (extra if supplemental else starting)[window][pixels]
        first = (supplements if supplemental else regions)[int(original[0])-1]
        if first['area'] != int(pixels.sum()) or not np.all(original == original[0]) or original[0] <= 0:
            raise ValueError('融合后的区域不是原候选')
        part = {'area': first['area'], 'supplement': bool(supplemental)}
        if first['state'] == 'measured':
            part.update({key: first[key] for key in PHYSICAL})
            if earlier is not None:
                part.update(temporal_fields(field, header, registered['registration'], observed, earlier, window))
        parts.append(part)
    metrics, geometry, assigned = object_scores(raster, parts, reference, [True]*count)
    if metrics != native_objects(fused, reference):
        raise ValueError('候选验证关联与既有对象指标不符')
    for i, part in enumerate(parts):
        part['matched'] = i in assigned
    return {'uri': row['uri'], 'time_utc': row['time_utc'], 'baseline': metrics,
            'candidates': parts, 'geometry': geometry, 'previous_state': state}


def optimize(fitting, policy, features):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    truth = [row['matched'] for row in fitting]
    if min(sum(truth), len(truth)-sum(truth)) < 15 or sum(row['matched'] for row in policy) < 20:
        raise ValueError('邻帧候选不足以拟合和设定保留率')
    estimator = make_pipeline(StandardScaler(), LogisticRegression(class_weight='balanced', C=1, random_state=2026))
    estimator.fit([[row[key] for key in features] for row in fitting], truth)
    positives = [[row[key] for key in features] for row in policy if row['matched']]
    scores = estimator.decision_function(positives)
    cut = float(np.quantile(scores, 1-RETENTION, method='lower'))
    assert (scores >= cut).mean() >= RETENTION
    scale, model = [entry[1] for entry in estimator.steps]
    return {'features': list(features), 'mean': scale.mean_.tolist(), 'scale': scale.scale_.tolist(),
            'weights': model.coef_[0].tolist(), 'intercept': float(model.intercept_[0]), 'review_threshold': cut}


def admissible(rows):
    return [part for row in rows for part in row['candidates'] if 'signed_correlation' in part]


def policy_masks(row, models):
    decisions = {}
    for name, model in models.items():
        keep, scores = [], []
        for part in row['candidates']:
            score, retain = decision({'state': 'measured', **part}, model) if 'signed_correlation' in part else (None, True)
            keep.append(retain)
            scores.append(score)
        decisions[name] = {'keep': keep, 'scores': scores}
    return decisions


def evaluate(rows, models):
    result = []
    for row in rows:
        objects = {'unchanged': row['baseline']}
        decisions = policy_masks(row, models)
        for name, selected in decisions.items():
            objects[name] = build_metrics(row['geometry'], selected['keep'])[0]
        result.append({**row, 'objects': objects, 'decisions': decisions})
    return result


def run(study, source, cohort, ranker, frozen, output):
    from sklearn.metrics import average_precision_score

    origin, known, plan, study, cohort = setup(study, source, cohort)
    ranker, frozen = Path(ranker).resolve(), Path(frozen).resolve()
    output = output_path(output, origin['dataset'])
    if (output/'summary.json').exists() or any(output.is_relative_to(path) for path in (study, cohort, ranker)):
        raise ValueError('邻帧复核需使用独立未完成目录')
    frozen_result = read(frozen)
    rank_summary = read(ranker/'summary.json')
    acquired = read(output/'intake.json')
    checkpoint = study/'hmi/run/last.pt'
    report = read(study/'summary.json')
    if (any(row['test_accessed'] or row['calibration_accessed'] for row in (report, frozen_result, acquired, rank_summary))
            or frozen_result['segmentation_summary_sha256'] != digest(study/'summary.json')
            or frozen_result['ranker_summary_sha256'] != digest(ranker/'summary.json')
            or digest(ranker/'rankers.json') != rank_summary['rankers_sha256']
            or acquired['cohort_sha256'] != digest(cohort/'summary.json')
            or acquired['state'] != 'completed' or report['checkpoint_sha256']['hmi'] != digest(checkpoint)):
        raise ValueError('已固定的分割、候选评分或邻帧接入身份不符')
    saved = load_checkpoint(checkpoint)
    if saved['identity']['code'] != plan['code'] or saved['epoch'] != plan['epochs']:
        raise ValueError('分割检查点身份不符')
    protocol = {
        'checkpoint_sha256': digest(checkpoint), 'frozen_supplement_sha256': digest(frozen),
        'intake_sha256': digest(output/'intake.json'), 'cohort_sha256': digest(cohort/'summary.json'),
        'code': {name: digest(Path(__file__).with_name(name)) for name in
                 ('ar_hmi_temporal_ranker.py', 'ar_hmi_candidate_neighbors.py', 'ar_hmi_candidate_ranker.py')},
        'scope': 'Earlier fitting, threshold, and held train periods. Segmentation had seen the underlying training set.',
        'retention': RETENTION, 'cutoffs': '50/25/25 by chronological 27-day blocks; adjacent boundary blocks excluded',
        'test_accessed': False, 'calibration_accessed': False, 'uses_future_observations': False,
    }
    identity = output/'protocol.json'
    if identity.is_file() and read(identity) != protocol:
        raise ValueError('已有邻帧复核数据协议改变')
    atomic_json(identity, protocol)
    data = HmiDataset(origin['dataset'], 'train', cohort, known, True)
    threshold = frozen_result['supplement_threshold']
    params = read(ranker/'rankers.json')['physical']
    models = {'starting': warm_model(origin, plan['config'])[0], 'hmi': expand(warm_model(origin, plan['config'])[0])}
    models['hmi'].load_state_dict(saved['model'])
    collected = []
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        seed_all(2026)
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        resources = Resources()
        for model in models.values():
            model.cuda().eval()
        with torch.inference_mode():
            for i, row in enumerate(data.rows):
                if (output/'PAUSE').is_file():
                    return {'state': 'paused', 'images_complete': i}
                artifact = output/'images'/f'{i:04d}.json'
                current = read(artifact)
                if current is None:
                    current = collect_item(data, i, models, params, threshold, acquired['rows'], output/'sources/hmi')
                    atomic_json(artifact, current)
                if current['uri'] != row['uri']:
                    raise ValueError('候选进度对应其他图像')
                collected.append(current)
                if (i+1) % 24 == 0:
                    atomic_json(output/'progress.json', {'state': 'extracting', 'complete':i+1, 'total':len(data)})
                    resources.sample()
        usage = resources.sample()
    partition = time_partition(collected)
    roles = {key: admissible(items) for key, items in partition.items()}
    populations = {key: {'images': len(partition[key]), 'temporal_candidates': len(items),
                        'matched': sum(item['matched'] for item in items)} for key, items in roles.items()}
    atomic_json(output/'coverage.json', {'roles': populations, 'source_states': dict(Counter(row['previous_state'] for row in collected))})
    models = {name: optimize(roles['fit'], roles['policy'], features)
              for name, features in (('spatial', SPATIAL), ('temporal', TEMPORAL))}
    held = evaluate(partition['held'], models)
    totals = {name: tally(held, name) for name in ('unchanged', *models)}
    p_ap = {}
    for name in models:
        pairs = [(part['matched'], score) for row in held
                 for part, score in zip(row['candidates'], row['decisions'][name]['scores'], strict=True) if score is not None]
        if len({label for label, _ in pairs}) == 2:
            p_ap[name] = float(average_precision_score(*zip(*pairs)))
    baseline, spatial, temporal = (totals[name] for name in ('unchanged', 'spatial', 'temporal'))
    gate = (temporal['region_precision']-baseline['region_precision'] >= .02
            and temporal['region_recall']-baseline['region_recall'] >= -.01
            and temporal['region_precision'] > spatial['region_precision']
            and temporal['under256']['matched'] >= baseline['under256']['matched']
            and {'spatial','temporal'} <= p_ap.keys() and p_ap['temporal'] > p_ap['spatial'])
    atomic_json(output/'held.json', held)
    atomic_json(output/'verifiers.json', models)
    result = {'state': 'completed', 'created_utc': now(), 'results': totals, 'candidate_ap': p_ap,
              'populations': populations, 'development_gate_passed': gate, 'resource': usage,
              'held_sha256': digest(output/'held.json'), 'verifiers_sha256': digest(output/'verifiers.json'),
              'protocol_sha256': digest(identity), 'pre_input_receipt_verified': False, 'deployed': False,
              'test_accessed': False, 'calibration_accessed': False}
    atomic_json(output/'summary.json', result)
    atomic_json(output/'progress.json', {'state':'completed','complete':len(data),'total':len(data)})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('study', 'source', 'cohort', 'ranker', 'frozen', 'output'):
        parser.add_argument('--'+key, required=True)
    configure_environment()
    output = run(**vars(parser.parse_args()))
    print(json.dumps({key: output[key] for key in ('state', 'results', 'candidate_ap', 'development_gate_passed') if key in output}))


if __name__ == '__main__':
    main()
