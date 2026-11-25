"""Evaluate isolated short bridges between existing AR mask components."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import binary_dilation, find_objects, label
from scipy.spatial import cKDTree
import torch
from torch.nn import functional as F

from .ar_continuum_study import expand
from .ar_hmi_candidate_ranker import candidates, decision, tally
from .ar_hmi_segmentation import HmiDataset, setup
from .ar_hmi_temporal_ranker import build_metrics, object_scores, time_partition
from .ar_native_tiles import add_isolated
from .ar_scale_review import native_objects
from .ar_scale_study import ROOT, warm_model
from .durable import atomic_json
from .journal import read
from .reviewed_train import load_checkpoint, seed_all
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


GAP_LIMIT = 8.
FEATURES = ('gap_pixels', 'size_ratio', 'small_log_area', 'large_log_area', 'bridge_gray_mean',
            'bridge_hmi_mean', 'bridge_gray_min', 'bridge_hmi_min')
POLICY_MINIMUM_PRECISION = .90


def components_in_range(first, second):
    return all(64 <= size for size in (first, second)) and any(256 <= size < 1024 for size in (first, second))


def close_pair(first_points, second_points):
    distances, neighbors = cKDTree(second_points).query(first_points, k=1)
    closest = int(np.argmin(distances))
    if distances[closest] > GAP_LIMIT:
        return None
    first = first_points[closest]
    second = second_points[neighbors[closest]]
    delta = second-first
    steps = int(abs(delta).sum())
    # A Manhattan stair-step joins 4-connected components even for diagonal endpoints.
    offsets = []
    for axis, direction in enumerate(delta):
        offsets.extend(((index+.5)/abs(direction), axis, int(np.sign(direction))) for index in range(abs(direction)))
    points = [first.copy()]
    for _, axis, step in sorted(offsets):
        nxt = points[-1].copy()
        nxt[axis] += step
        points.append(nxt)
    assert len(points) == steps+1 and np.array_equal(points[-1], second)
    return float(distances[closest]), np.array(points)


def bridge_points(raster, points, components):
    left, top = np.maximum(0, points.min(axis=0)-1)
    right, bottom = np.minimum(raster.shape, points.max(axis=0)+2)
    touch = np.zeros((right-left, bottom-top), dtype=bool)
    touch[tuple((points - (left, top)).T)] = True
    local = raster[left:right, top:bottom]
    if set(np.unique(local[binary_dilation(touch)])) - {0, *components}:
        return None
    pixels = points[raster[tuple(points.T)] == 0]
    return pixels if len(pixels) else None


def overlaps(raster, first, second, sizes, pixels, reference, geometry):
    # Compare local object geometry with identical IoU matching before and after a bridge.
    ids = np.asarray(geometry['reference_ids'])
    ref_sizes = np.asarray(geometry['reference_sizes'])
    a, b = sizes[first-1], sizes[second-1]
    before = np.asarray(geometry['iou'])[[first-1, second-1]]
    hit_a, hit_b = np.rint(before*(np.array([a,b])[:,None]+ref_sizes)/(1+before))
    bridge_hits = reference[tuple(pixels.T)]
    hits = hit_a+hit_b+np.count_nonzero(bridge_hits[:, None] == ids, axis=0)
    joined = hits / (a+b+len(pixels)+ref_sizes-hits)
    return joined


def pair_features(raster, probabilities, reference, geometry):
    windows = find_objects(raster)
    sizes = np.bincount(raster.ravel())[1:]
    choices = []
    for i, first in enumerate(windows, 1):
        for j, second in enumerate(windows[i:], i+1):
            if not components_in_range(sizes[i-1], sizes[j-1]):
                continue
            if any(max(a.start-b.stop+1, b.start-a.stop+1, 0) > GAP_LIMIT for a,b in zip(first, second, strict=True)):
                continue
            point_arrays = [(np.argwhere(raster[window] == identity) + [axis.start for axis in window])
                            for identity, window in ((i, first), (j, second))]
            closest = close_pair(*point_arrays)
            if closest is None:
                continue
            gap, points = closest
            pixels = bridge_points(raster, points, (i,j))
            if pixels is None:
                continue
            a, b = sorted((int(sizes[i-1]), int(sizes[j-1])))
            values = [view[tuple(pixels.T)] for view in probabilities]
            row = {'first': i-1, 'second': j-1, 'gap_pixels': gap, 'size_ratio': a/b, 'small_log_area': float(np.log1p(a)),
                   'large_log_area': float(np.log1p(b)), 'bridge_gray_mean': float(values[0].mean()),
                   'bridge_hmi_mean': float(values[1].mean()), 'bridge_gray_min': float(values[0].min()),
                   'bridge_hmi_min': float(values[1].min()), 'bridge_yx': pixels.tolist()}
            row['iou_after'] = overlaps(raster, i, j, sizes, pixels, reference, geometry).tolist()
            choices.append(row)
    return choices


def outcome(geometry, chosen):
    rows = [list(row) for row in geometry['iou']]
    removed = set()
    for pair in chosen:
        rows[pair['first']] = pair['iou_after']
        removed.add(pair['second'])
    return build_metrics({**geometry, 'iou': rows}, [i not in removed for i in range(len(rows))])[0]


def select(options, ranked):
    selected, used = [], set()
    for i in ranked:
        pair = options[i]
        ids = {pair['first'], pair['second']}
        pixels = {tuple(pixel) for pixel in pair['bridge_yx']}
        neighbors = {(y+dy,x+dx) for y,x in pixels for dy,dx in ((0,0),(-1,0),(1,0),(0,-1),(0,1))}
        if ids & used or any(neighbors & other for _,other in selected):
            continue
        selected.append((pair,pixels))
        used.update(ids)
    return [pair for pair,_ in selected]


def with_reference(corrected, baseline):
    old, new = (set(a['matched_reference_ids']) for a in (baseline, corrected))
    # A merger is useful only with a verified correct output and no lost old match.
    return old <= new


def evaluate_pair_labels(geometry, baseline, pairs):
    ids = geometry['reference_ids']
    old = set(baseline['matched_reference_ids'])
    for pair in pairs:
        after = outcome(geometry, [pair])
        eligible = {identity for identity, iou in zip(ids, pair['iou_after'], strict=True) if iou >= .5}
        new = set(after['matched_reference_ids'])
        pair['safe_repair'] = bool(old <= new and eligible & new)
        pair['new_matches'] = len(new-old)
        pair['lost_matches'] = len(old-new)
    return pairs


def probability(model, tensor, shape):
    with torch.autocast('cuda', dtype=torch.float16):
        logits = model(tensor)
    return F.interpolate(logits.float().sigmoid(), size=shape, mode='bilinear', align_corners=False)[0,0].cpu().numpy()


def collect(data, index, models, params, cut):
    item, row = data[index], data.rows[index]
    phys = data.pairs[row['uri']]
    with Image.open(data.source(row['uri'])) as source:
        gray = np.array(source.convert('L'))
    ref = np.load(data.source(row['mask_uri']), allow_pickle=False)
    field = np.load(data.cohort/'pairs'/phys['id']/'aligned-los.npy', allow_pickle=False) if item['physical_used'] else None
    tensor = item['image'][None].cuda()
    scores = (probability(models[0], tensor[:, :1], ref.shape), probability(models[1], tensor, ref.shape))
    _, first, parts = candidates(scores[0], ref, field, gray)
    accepted = np.asarray([False, *(p['area'] >= 512 or decision(p, params)[1] for p in parts)])[first]
    _, second, additional = candidates(scores[1], ref, field, gray)
    extra = [False]
    for part in additional:
        score, _ = decision(part, params)
        extra.append(part['area'] < 512 and score is not None and score >= cut)
    original = add_isolated(accepted, np.asarray(extra)[second])
    raster, count = label(original)
    parts = [{'area': size} for size in np.bincount(raster.ravel(), minlength=count+1)[1:].tolist()]
    baseline, geometry, _ = object_scores(raster, parts, ref, [True]*count)
    if baseline != native_objects(original, ref):
        raise ValueError('候选几何基线与既有对象评价不一致')
    pairs = evaluate_pair_labels(geometry, baseline, pair_features(raster, scores, ref, geometry))
    return {'uri': row['uri'], 'time_utc': row['time_utc'], 'baseline': baseline, 'pairs': pairs, 'geometry': geometry}


def fit(train):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    parts = time_partition(train)
    examples = {role: [pair for row in rows for pair in row['pairs']] for role,rows in parts.items()}
    counts = {role: {'images':len(parts[role]), 'pairs':len(rows), 'safe_repairs':sum(p['safe_repair'] for p in rows)}
              for role,rows in examples.items()}
    truth = [p['safe_repair'] for p in examples['fit']]
    if min(sum(truth), len(truth)-sum(truth)) < 10 or counts['policy']['safe_repairs'] < 10:
        return None, counts
    x = lambda rows: [[p[k] for k in FEATURES] for p in rows]
    model = make_pipeline(StandardScaler(), LogisticRegression(C=1, class_weight='balanced',random_state=2026))
    model.fit(x(examples['fit']), truth)
    decisions = model.decision_function(x(examples['policy']))
    labels = np.asarray([p['safe_repair'] for p in examples['policy']])
    feasible = []
    for cut in np.unique(decisions):
        use = decisions >= cut
        if use.sum() >= 10 and labels[use].mean() >= POLICY_MINIMUM_PRECISION:
            feasible.append((int(use.sum()), float(cut)))
    if not feasible:
        return None, counts
    n, cut = max(feasible)
    scale, classifier = [estimator for _,estimator in model.steps]
    return {'features': list(FEATURES), 'mean': scale.mean_.tolist(), 'scale': scale.scale_.tolist(),
            'weights': classifier.coef_[0].tolist(), 'intercept': float(classifier.intercept_[0]),
            'review_threshold': cut, 'policy_accepted': n}, counts


def evaluate(row, model):
    baseline,pairs,geometry = (row[key] for key in ('baseline','pairs','geometry'))
    ranked = sorted((i for i,pair in enumerate(pairs) if pair['safe_repair']),
                    key=lambda i: (-pairs[i]['new_matches'], pairs[i]['gap_pixels'], i))
    oracle = select(pairs, ranked)
    options = []
    if model is not None:
        for i,pair in enumerate(pairs):
            score, accepted = decision({'state':'measured', **pair},model)
            if accepted:
                options.append((i,score))
    chosen = select(pairs, [i for i,_ in sorted(options,key=lambda r: (-r[1],r[0]))])
    results = {'baseline':baseline,'oracle_short_bridges':outcome(geometry,oracle),'model_short_bridges':outcome(geometry,chosen)}
    return {**row,'objects':results,'oracle_bridges':len(oracle),'model_bridges':len(chosen),
            'oracle_safe_in_combination':with_reference(results['oracle_short_bridges'],baseline)}


def run(study, source, cohort, ranker, frozen, output):
    origin, known, plan, study, cohort = setup(study, source, cohort)
    ranker, frozen = Path(ranker).resolve(), Path(frozen).resolve()
    output = output_path(output, origin['dataset'])
    if (output/'summary.json').is_file() or any(output.is_relative_to(path) for path in (study,cohort,ranker)):
        raise ValueError('新修复对照需要独立目录')
    report, rank_summary, supplement = (read(path) for path in (study/'summary.json', ranker/'summary.json', frozen))
    checkpoint = study/'hmi/run/last.pt'
    if (any(e['test_accessed'] or e['calibration_accessed'] for e in (report,rank_summary,supplement))
            or supplement['segmentation_summary_sha256'] != digest(study/'summary.json')
            or supplement['ranker_summary_sha256'] != digest(ranker/'summary.json')
            or rank_summary['rankers_sha256'] != digest(ranker/'rankers.json')
            or report['checkpoint_sha256']['hmi'] != digest(checkpoint)
            or supplement['images_sha256'] != digest(frozen.with_name('images.json'))):
        raise ValueError('原候选规则身份变化')
    expected = {row['uri']:row['objects']['starting_with_supplement'] for row in read(frozen.with_name('images.json'))}
    protocol = {'baseline_sha256':digest(frozen),'checkpoint_sha256':digest(checkpoint),'cohort_sha256':digest(cohort/'summary.json'),
                'code_sha256':digest(__file__),'gap_limit_pixels':GAP_LIMIT,'matched_iou':.5,'validation_compared_after_fit':True,
                'policy_precision_requirement':POLICY_MINIMUM_PRECISION,'test_accessed':False,'calibration_accessed':False}
    marker = output/'protocol.json'
    if marker.is_file() and read(marker) != protocol:
        raise ValueError('中断后冻结条件改变')
    atomic_json(marker,protocol)
    saved = load_checkpoint(checkpoint)
    if saved['identity']['code'] != plan['code'] or saved['epoch'] != plan['epochs']:
        raise ValueError('冻结检查点身份不符')
    fresh = lambda: warm_model(origin,plan['config'])[0]
    models = (fresh(),expand(fresh()))
    models[1].load_state_dict(saved['model'])
    params = read(ranker/'rankers.json')['physical']
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        seed_all(2026)
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        resource = Resources()
        for model in models:
            model.cuda().eval()
        groups = {}
        with torch.inference_mode():
            for split in ('train','validation'):
                if split == 'validation':
                    verifier, fit_counts = fit(groups['train'])
                    atomic_json(output/'threshold.json',{'model':verifier,'fitting':fit_counts})
                data = HmiDataset(origin['dataset'],split,cohort,known,True)
                rows = []
                for i,row in enumerate(data.rows):
                    if (output/'PAUSE').is_file():
                        return {'state':'paused','split':split,'complete':i}
                    cache = output/split/f'{i:04d}.json'
                    current = read(cache)
                    if current is None:
                        current = collect(data,i,models,params,supplement['supplement_threshold'])
                        atomic_json(cache,current)
                    if current['uri'] != row['uri'] or split == 'validation' and current['baseline'] != expected[row['uri']]:
                        raise ValueError('活动区固定候选未复现')
                    rows.append(current)
                    if (i+1)%24 == 0:
                        atomic_json(output/'progress.json',{'state':'collecting','split':split,'complete':i+1,'total':len(data)})
                        resource.sample()
                groups[split] = rows
        usage = resource.sample()
    compared = [evaluate(row,verifier) for row in groups['validation']]
    names = ('baseline','oracle_short_bridges','model_short_bridges')
    complete = {name:tally(compared,name) for name in names}
    atomic_json(output/'development.json',compared)
    result = {'state':'completed','created_utc':now(),'train_images':len(groups['train']), 'development_images':len(compared),
              'fitting':fit_counts,'verifier_available':verifier is not None,
              'development_options':sum(len(r['pairs']) for r in compared),
              'selected_oracle_bridges':sum(r['oracle_bridges'] for r in compared),
              'oracle_images_with_lost_matches':sum(not r['oracle_safe_in_combination'] for r in compared),
              'selected_model_bridges':sum(r['model_bridges'] for r in compared),
              'results':complete,'resource':usage,'protocol_sha256':digest(marker),
              'development_sha256':digest(output/'development.json'),
              'test_accessed':False,'calibration_accessed':False,'deployed':False}
    atomic_json(output/'summary.json',result)
    atomic_json(output/'progress.json',{'state':'completed','split':'validation','complete':len(compared)})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('study','source','cohort','ranker','frozen','output'):
        parser.add_argument('--'+key,required=True)
    configure_environment()
    result = run(**vars(parser.parse_args()))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
