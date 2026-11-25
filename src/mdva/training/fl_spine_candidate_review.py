"""Verify independently whether weak FL spine proposals can be screened reliably."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

import cv2
import numpy as np
from safetensors.torch import load_file
from scipy.ndimage import binary_dilation, find_objects, label
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
import torch

from .ar_hmi_temporal_ranker import build_metrics, object_scores
from .durable import atomic_json
from .fl_author_model import AuthorGraph
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT
from .fl_gong_patch_training import image_bytes
from .fl_gong_resolution import predict
from .fl_magfilo_adaptation import source_receipt
from .fl_magfilo_audit import raster
from .fl_magfilo_transfer import inventory, load_gray
from .fl_spine_context_study import architecture, context, full_score, locate
from .journal import read
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


def candidate_data(gray, local, line, additions):
    regions, _ = label(additions, structure=CONNECTIVITY)
    measurements = []
    for key, (y, x) in enumerate(find_objects(regions), 1):
        owns = regions[y, x] == key
        g = gray[y, x]
        mask, spine = local[y, x][owns], line[y, x][owns]
        coords = np.column_stack(np.nonzero(owns)).astype(float)
        center = coords - coords.mean(axis=0)
        minor, major = np.linalg.eigvalsh(center.T @ center / len(coords))
        other = binary_dilation(owns, structure=CONNECTIVITY, iterations=3) & ~owns
        contrast = float(np.mean(g[other]) - np.mean(g[owns])) if other.any() else 0.
        measurements.append([float(np.log1p(len(coords))), float(np.log((major + .25) / (minor + .25))),
                             float(mask.mean()), float(mask.max()), float(spine.mean()), float(spine.max()),
                             float((spine >= .35).mean()), contrast, float(len(coords) / owns.size)])
    return regions, measurements


def geometry_record(gray, whole, local, spine, ground):
    additions = locate(local, spine, whole)
    isolated, x = candidate_data(gray, local, spine, additions)
    previous, p = label(whole, structure=CONNECTIVITY)
    joined = np.where(isolated > 0, isolated + p, previous)
    parts = [{'area': int(size)} for size in np.bincount(joined.ravel())[1:]]
    geometry, expected = [], np.zeros(len(x), dtype=bool)
    oracle = 0
    for truth in ground:
        labels = label(truth, structure=CONNECTIVITY)[0]
        g = object_scores(joined, parts, labels, [True] * p + [False] * len(x))[1]
        iou = np.asarray(g['iou'], dtype=float).reshape(p + len(x), len(g['reference_ids']))
        missed = iou[:p].max(axis=0, initial=0.) == 0
        new = iou[p:, missed] >= .50
        expected |= new.any(axis=1)
        oracle += int(new.any(axis=0).sum())
        geometry.append(g)
    return {'base_count':p, 'features':x, 'expected':expected.tolist(), 'variants':geometry, 'oracle_strict_misses':oracle}


def measures(rows, model=None, cutoff=None, mode='baseline'):
    aggregate = Counter()
    for r in rows:
        selected = np.full(len(r['features']), mode == 'unfiltered', dtype=bool)
        if mode == 'reviewed' and len(selected):
            selected = model.decision_function(r['features']) >= cutoff
        for g in r['variants']:
            score = build_metrics(g, [True] * r['base_count'] + selected.tolist())[0]
            aggregate.update({k:score[k] for k in ('matched','reference','predicted')})
    m, r, n = (aggregate[k] for k in ('matched','reference','predicted'))
    return {**aggregate, 'precision':m/max(n,1), 'recall':m/max(r,1), 'unmatched':n-m}


def selection(rows):
    ordered = sorted(rows, key=lambda r:r['file'])
    boundary = ordered[len(ordered)*2//3]['file'][:8]
    fit = [r for r in ordered if r['file'][:8] < boundary]
    tune = [r for r in ordered if r['file'][:8] >= boundary]
    x = [f for r in fit for f in r['features']]
    y = [v for r in fit for v in r['expected']]
    info = {'dates_fit':len({r['file'][:8] for r in fit}), 'dates_policy':len({r['file'][:8] for r in tune}),
            'candidates_fit':len(x), 'strict_miss_candidates_fit':sum(y),
            'strict_miss_candidates_policy':sum(sum(r['expected']) for r in tune)}
    if min(sum(y), len(y)-sum(y)) < 12 or info['strict_miss_candidates_policy'] < 3:
        return None, None, {**info, 'state':'not_enough_corroborated_candidates'}
    model = make_pipeline(StandardScaler(), LogisticRegression(C=1, class_weight='balanced', random_state=2026)).fit(x,y)
    cutoffs = np.unique(np.concatenate([model.decision_function(r['features']) for r in tune if r['features']]))
    if len(cutoffs) > 100:
        cutoffs = np.quantile(cutoffs, np.linspace(0,1,100))
    base = measures(tune)
    eligible = []
    for cutoff in cutoffs:
        s = measures(tune, model, cutoff, 'reviewed')
        gained, noisy = s['matched']-base['matched'], s['unmatched']-base['unmatched']
        if gained >= 3 and noisy <= gained:
            eligible.append((gained, -noisy, float(cutoff)))
    if not eligible:
        return model, None, {**info,'state':'marginal_precision_gate_failed'}
    n, noisy, cutoff = max(eligible)
    return model, cutoff, {**info,'state':'completed','cutoff':cutoff,'policy_additions_matched':n,'policy_extra_unmatched':-noisy}


def run(source, release, development, trained, anchor, output):
    output = output_path(output, DATASET)
    if output.exists() or any(output.is_relative_to(p.resolve()) for p in (development, trained, anchor)):
        raise ValueError('主轴复核需要独立目录')
    prior = read(trained / 'summary.json')
    if prior['protocol_sha256'] != digest(trained/'protocol.json') or prior['cases_sha256'] != digest(trained/'cases.json'):
        raise ValueError('已完成的主轴实验身份不同')
    rows, table = inventory(source,release)
    inputs = source_receipt(development,'transfer_dev',{r['file']:r for r in rows})
    if any(not r['file'].startswith(('2017','2018')) for r in inputs):
        raise ValueError('只读原有开发日期')
    model_file = trained/'mask_and_spine/research.safetensors'
    baseline_file = anchor/'mixed_gong/last.pt'
    if digest(baseline_file) != read(anchor/'summary.json')['results']['mixed_gong']['weights_sha256']:
        raise ValueError('全图对照身份不同')
    cv2.setNumThreads(2)
    torch.set_num_threads(2)
    plan = output/'protocol.json'
    atomic_json(plan,{'code_sha256':digest(__file__),'prior_sha256':digest(trained/'summary.json'),
                     'weights_sha256':[digest(model_file),digest(baseline_file)],'fit':'early 2017 dates','tune':'remaining 2017 dates',
                     'research_check':'2018 development dates','test_accessed':False,'year_2022_accessed':False})
    records = []
    with exclusive(ROOT/'.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cuda.matmul.allow_tf32 = False
        base, joint = AuthorGraph('batch').cuda().eval(), architecture().cuda().eval()
        base.load_state_dict(load_checkpoint(baseline_file)['model'])
        joint.load_state_dict(load_file(model_file))
        usage = Resources()
        for row in inputs:
            if (output/'PAUSE').exists():
                atomic_json(output/'status.json',{'state':'paused','processed':len(records)})
                return {'state':'paused','processed':len(records)}
            raw = image_bytes(development,row)
            gray = load_gray(raw)
            x, valid = context(raw)
            coarse = predict(base,gray[None])[0] >= .50
            segmentation, spine = full_score(joint,x,valid)
            masks = [raster(table[v['id']]) for v in row['annotators']]
            records.append({'file':row['file'], **geometry_record(gray,coarse,segmentation,spine,masks)})
            if len(records) % 15 == 0:
                atomic_json(output/'status.json',{'state':'evaluating','processed':len(records),'total':len(inputs)})
                usage.sample()
        resources = usage.sample()
    fitting = [r for r in records if r['file'].startswith('2017')]
    confirm = [r for r in records if r['file'].startswith('2018')]
    baseline = measures(confirm)
    unfiltered = measures(confirm,mode='unfiltered')
    for name, score in (('whole',baseline),('whole_plus_spine_proposals',unfiltered)):
        known = prior['metrics'][name]
        if [score[k] for k in ('matched','predicted','reference')] != [known[k] for k in ('matched_iou_050','predicted_regions_total_across_variants','reference_regions_total_across_variants')]:
            raise ValueError('主轴先前参考结果未复现')
    model, cutoff, info = selection(fitting)
    final = measures(confirm,model,cutoff,'reviewed') if cutoff is not None else None
    atomic_json(output/'objects.json',records)
    result = {'state':'completed','recorded_utc':now(),'fit_2017':len(fitting),'check_2018':len(confirm),'policy':info,
              'check_baseline':baseline,'check_unfiltered':unfiltered,'check_selected':final,
              'check_strict_recoverable_zero_overlap':sum(r['oracle_strict_misses'] for r in confirm),'resources':resources,
              'protocol_sha256':digest(plan),'objects_sha256':digest(output/'objects.json'),'deployed':False,'test_accessed':False,'year_2022_accessed':False}
    atomic_json(output/'summary.json',result)
    atomic_json(output/'status.json',{'state':'completed','processed':len(records),'total':len(inputs)})
    return result


if __name__ == '__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source','release','development','trained','anchor','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    print(json.dumps(run(**vars(parser.parse_args())),ensure_ascii=False))
