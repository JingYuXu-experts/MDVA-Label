"""Refit the candidate reviewer for the frozen whole-disk plus crop predictor."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import label
import torch

from .ar_hmi_temporal_ranker import object_scores
from .durable import atomic_json
from .fl_author_model import AuthorGraph
from .fl_candidate_review import apply_review, aggregate, choose_cutoff, describe, features, fit_model, score_row
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT
from .fl_gong_patch_training import make_input
from .fl_gong_resolution import GRID, predict, resize, tiles_for
from .fl_magfilo_adaptation import source_receipt
from .fl_magfilo_audit import raster
from .fl_magfilo_transfer import inventory, load_gray
from .journal import read
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


def measure(whole_model, crop_model, raw, refs, row):
    gray = load_gray(raw)
    whole = predict(whole_model, gray[None])[0]
    crop = tiles_for(crop_model, make_input(raw, True))
    fused = resize(.65 * resize(whole, GRID) + .35 * crop, 512)
    regions, inputs = describe(gray, fused)
    areas = [{'area': int(size)} for size in np.bincount(regions.ravel())[1:]]
    selected = np.ones(len(areas), dtype=bool)
    geometry = [object_scores(regions, areas, label(ref, structure=CONNECTIVITY)[0], selected)[1] for ref in refs]
    return {'name': row['file'], 'time': row['captured_utc'], 'station': row['file'][14:16],
            'features': inputs.tolist(), 'geometry': geometry}


def run(source, release, early, later, adapted, patches, prior, output):
    torch.set_num_threads(2)
    output = output_path(output, DATASET)
    if output.exists() or any(output.is_relative_to(p.resolve()) for p in (early, later, adapted, patches, prior)):
        raise ValueError('须使用新目录保存融合候选复核')
    original, patch_report, reviewer = (read(p / 'summary.json') for p in (adapted, patches, prior))
    if (any(r['state'] != 'completed' or r['test_accessed'] or r['calibration_accessed']
            for r in (original, patch_report, reviewer))
            or not patch_report['passed'] or patch_report['external_2021_accessed']
            or any(r['protocol_sha256'] != digest(p / 'protocol.json') for r, p in
                   zip((original, patch_report, reviewer), (adapted, patches, prior), strict=True))):
        raise ValueError('冻结方案的状态或校验和不符')
    whole_weight = adapted / 'mixed_gong' / 'last.pt'
    crop_weight = patches / 'detail_training' / 'last.pt'
    whole_sha, crop_sha = digest(whole_weight), digest(crop_weight)
    if (whole_sha != original['results']['mixed_gong']['weights_sha256']
            or whole_sha != patch_report['result']['whole']['checkpoint_sha256']
            or crop_sha != patch_report['result']['detail_training']['checkpoint_sha256']):
        raise ValueError('分割权重身份不符')
    registered, anns = inventory(source, release)
    index = {row['file']: row for row in registered}
    train = source_receipt(early, 'train_available', index)
    dev = source_receipt(later, 'transfer_dev', index)
    prior_training = set(read(adapted / 'protocol.json')['gong_training_images'])
    prior_training.update(row['file'] for row in read(patches / 'protocol.json')['samples'])
    available = [r for r in train if r['file'] not in prior_training]
    roles = {
        'fit': [r for r in available if r['captured_utc'] < '2014'],
        'policy': [r for r in available if r['captured_utc'].startswith('2015')],
        'development': dev,
    }
    if len(roles['fit']) < 80 or len(roles['policy']) < 24 or len(dev) != 90:
        raise ValueError('复核模型的互斥训练期输入不足')
    plan = {
        'code_sha256': digest(__file__), 'weights_sha256': {'whole': whole_sha, 'crop': crop_sha},
        'fixed_blend': [.65, .35], 'pixel_threshold': .5, 'policy_match_retention': .98,
        'samples': {k:[r['file'] for r in rows] for k, rows in roles.items()},
        'patch_protocol_sha256': patch_report['protocol_sha256'], 'prior_review_sha256': digest(prior / 'summary.json'),
        'development_gate': 'relative to unfiltered blend: precision +5pp, recall loss <=1pp; '
                            'relative to reviewed whole: precision loss <=1pp and recall gain >=2pp',
        'test_accessed': False, 'calibration_accessed': False, 'independent_accessed': False,
    }
    atomic_json(output / 'protocol.json', plan)
    whole, crop = AuthorGraph('batch'), AuthorGraph('batch')
    whole.load_state_dict(load_checkpoint(whole_weight)['model'])
    crop.load_state_dict(load_checkpoint(crop_weight)['model'])
    collected = {}
    with exclusive(ROOT / '.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cuda.matmul.allow_tf32 = False
        whole.cuda().eval()
        crop.cuda().eval()
        usage = Resources()
        for role, images in roles.items():
            folder = later if role == 'development' else early
            results = []
            for i, row in enumerate(images):
                raw = (folder / 'jpeg' / row['file']).read_bytes()
                if sha256(raw).hexdigest() != row['image_sha256']:
                    raise ValueError('融合复核图像已改变')
                refs = [raster(anns[a['id']]) for a in row['annotators']]
                results.append(measure(whole, crop, raw, refs, row))
                if i % 16 == 0:
                    atomic_json(output / 'progress.json', {'role': role, 'images': i + 1, 'total': len(images)})
                    usage.sample()
            collected[role] = results
        use = usage.sample()
    fitted = fit_model(collected['fit'])
    cuts = {'probability': choose_cutoff(collected['policy'], lambda row: features(row)[:, 5]),
            'shape_and_brightness': choose_cutoff(collected['policy'], lambda row: score_row(row, fitted))}
    rules = {'classifier': fitted, 'cutoffs': cuts}
    atomic_json(output / 'rules.json', rules)
    dev_rows = [apply_review(row, fitted, cuts) for row in collected['development']]
    atomic_json(output / 'development.json', dev_rows)
    result = aggregate(dev_rows)
    old = reviewer['results']['gong']['dev']['shape_and_brightness']
    unfiltered, fused = (result[k] for k in ('original', 'shape_and_brightness'))
    target = patch_report['result']['detail_training']['fixed_grid']
    if (target['matched_iou_050'] != unfiltered['matched']
            or target['predicted_regions_total_across_variants'] != unfiltered['predicted']):
        raise ValueError('融合基线的区域统计没有精确复现')
    gates = {
        'fewer_false_candidates': fused['precision'] >= unfiltered['precision'] + .05,
        'fusion_recall_retained': fused['recall'] >= unfiltered['recall'] - .01,
        'whole_review_precision_retained': fused['precision'] >= old['precision'] - .01,
        'whole_review_recall_gain': fused['recall'] >= old['recall'] + .02,
    }
    report = {
        'state': 'completed', 'recorded_utc': now(), 'passed': all(gates.values()), 'checks': gates,
        'counts': {k:len(v) for k,v in roles.items()}, 'prior_fixed_whole_review': old, 'development': result,
        'policy': aggregate([apply_review(row, fitted, cuts) for row in collected['policy']]),
        'weights_sha256': plan['weights_sha256'], 'rule_sha256': digest(output / 'rules.json'),
        'development_sha256': digest(output / 'development.json'), 'protocol_sha256': digest(output / 'protocol.json'),
        'resources': use, 'deployed': False, 'test_accessed': False, 'calibration_accessed': False,
    }
    atomic_json(output / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'release', 'early', 'later', 'adapted', 'patches', 'prior', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    configure_environment()
    result = run(**vars(parser.parse_args()))
    print(json.dumps({k: result[k] for k in ('state', 'passed', 'checks', 'counts', 'development')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
