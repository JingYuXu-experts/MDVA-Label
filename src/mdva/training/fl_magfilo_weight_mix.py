"""Measure whether interpolating the original and GONG-adapted weights preserves FL performance."""

from __future__ import annotations

import argparse
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
from safetensors.torch import save
from scipy.ndimage import label
import torch

from .datasets import SegmentationDataset
from .diagnostic_support import connected_scores
from .durable import atomic_json, commit
from .fl_author_model import AuthorGraph
from .fl_ensemble_study import CONNECTIVITY, DATASET, PILOT, ROOT
from .fl_magfilo_adaptation import prediction, source_receipt
from .fl_magfilo_audit import compare, raster
from .fl_magfilo_transfer import inventory, load_gray, metrics
from .fl_pilot_review import region_summary
from .journal import read
from .metrics import binary_segmentation, segmentation_summary
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


FRACTIONS = (0., .25, .5, .75, 1.)
PRIMARY = .5


def blend(original, adapted, parameters, fraction):
    if original.keys() != adapted.keys() or not parameters <= original.keys():
        raise ValueError('原模型与适配模型的参数结构不同')
    proposed = {}
    for name, before in original.items():
        after = adapted[name]
        if before.shape != after.shape or before.dtype != after.dtype:
            raise ValueError(f'模型张量结构变化 {name}')
        if name in parameters:
            if not (before.is_floating_point() and torch.isfinite(before).all() and torch.isfinite(after).all()):
                raise ValueError(f'模型参数超过插值数值范围 {name}')
            proposed[name] = before if fraction == 0 else after if fraction == 1 else torch.lerp(before, after, fraction)
        else:
            if not torch.equal(before, after):
                raise ValueError(f'比较要求归一化缓存保持一致 {name}')
            proposed[name] = before
    return proposed


def evaluate(model, local, external, folder, annotations, resource):
    rows = {'local': [], 'gong': []}
    for i, row in enumerate(local.rows):
        item = local[i]
        native = item['semantic_mask'][0].numpy() > 0
        mask = prediction(model, item['image'][None])
        ref = label(native, structure=CONNECTIVITY)[0]
        rows['local'].append({
            'uri': row['uri'], 'station': row['instrument'], 'time_utc': row['time_utc'],
            'pixels': binary_segmentation(mask, native), 'regions': connected_scores(mask, ref),
        })
        if i % 48 == 0:
            resource.sample()
    for row in external:
        raw = (folder / 'jpeg' / row['file']).read_bytes()
        if sha256(raw).hexdigest() != row['image_sha256']:
            raise ValueError('GONG 开发图像身份变化')
        mask = prediction(model, torch.from_numpy(load_gray(raw))[None, None])
        rows['gong'].append({
            'file': row['file'], 'time_utc': row['captured_utc'],
            'comparisons': [compare(mask, raster(annotations[a['id']])) for a in row['annotators']],
        })
    return rows


def result_for(rows):
    station_rows = defaultdict(list)
    for row in rows['local']:
        station_rows[row['station']].append(row['pixels'])
    return {
        'local': {
            'pixels': segmentation_summary([row['pixels'] for row in rows['local']]),
            'regions': region_summary([row['regions'] for row in rows['local']]),
            'station_dice': {name: segmentation_summary(pixels)['dice'] for name, pixels in station_rows.items()},
        },
        'gong': metrics(rows['gong']),
    }


def meets_gate(candidate, original, adapted):
    local, gong = candidate['local'], candidate['gong']
    before_l, before_g = original['local'], original['gong']
    after_g = adapted['gong']
    return {
        'local_recall_within_1pp': local['regions']['recall'] >= before_l['regions']['recall'] - .01,
        'local_precision_within_1pp': local['regions']['precision'] >= before_l['regions']['precision'] - .01,
        'local_dice_within_1pp': local['pixels']['dice'] >= before_l['pixels']['dice'] - .01,
        'local_stations_dice_within_3pp': all(
            local['station_dice'][station] >= dice - .03 for station, dice in before_l['station_dice'].items()
        ),
        'gong_retains_70pct_recall_gain': gong['recall_iou_050'] >= (
            before_g['recall_iou_050'] + .70 * (after_g['recall_iou_050'] - before_g['recall_iou_050'])
        ),
        'gong_retains_70pct_precision_gain': gong['precision_iou_050'] >= (
            before_g['precision_iou_050'] + .70 * (after_g['precision_iou_050'] - before_g['precision_iou_050'])
        ),
    }


def run(source, release, later, adapted, output):
    output = output_path(output, DATASET)
    if output.exists() or any(output.is_relative_to(p.resolve()) for p in (later, adapted)):
        raise ValueError('参数插值需要新输出目录')
    base_report = read(PILOT / 'summary.json')
    study = read(adapted / 'summary.json')
    if any(r['state'] != 'completed' or r['test_accessed'] or r['calibration_accessed'] for r in (base_report, study)):
        raise ValueError('参数插值只接受已登记的开发模型')
    protocol_file = adapted / 'protocol.json'
    study_protocol = read(protocol_file)
    original_file = PILOT / 'checkpoint.pt'
    tuned_file = adapted / 'mixed_gong' / 'last.pt'
    original_sha = digest(original_file)
    tuned_sha = digest(tuned_file)
    if (original_sha != base_report['checkpoint_sha256'] or original_sha != study_protocol['warm_start_sha256']
            or tuned_sha != study['results']['mixed_gong']['weights_sha256']
            or study['protocol_sha256'] != digest(protocol_file)
            or study_protocol['later_inventory_sha256'] != digest(later / 'inventory.json')):
        raise ValueError('参数插值的权重或开发集身份不符')
    original, tuned = (load_checkpoint(path)['model'] for path in (original_file, tuned_file))
    registered, annotations = inventory(source, release)
    external = source_receipt(later, 'transfer_dev', {r['file']: r for r in registered})
    local = SegmentationDataset(DATASET, 'FL', 'validation', 512)
    if len(local) != 182 or len(external) != 90:
        raise ValueError('开发集样本数不符')
    protocol = {
        'source_sha256': digest(__file__), 'original_weights_sha256': original_sha, 'adapted_weights_sha256': tuned_sha,
        'fractions': FRACTIONS, 'primary': PRIMARY, 'threshold': .5, 'train_executed': False,
        'other_fractions': 'diagnostics only', 'local_images': len(local), 'gong_images': len(external),
        'local_gate': 'precision, recall and Dice loss <=1pp, per-station Dice loss <=3pp',
        'gong_gate': 'at least 70% of observed strict precision/recall gain retained',
        'test_accessed': False, 'calibration_accessed': False,
    }
    atomic_json(output / 'protocol.json', protocol)
    results = {}
    model = AuthorGraph('batch')
    parameters = {name for name, _ in model.named_parameters()}
    with exclusive(ROOT / '.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        model.cuda().eval()
        resources = Resources()
        for fraction in FRACTIONS:
            model.load_state_dict(blend(original, tuned, parameters, fraction))
            rows = evaluate(model, local, external, later, annotations, resources)
            results[fraction] = result_for(rows)
            atomic_json(output / f'alpha-{fraction:.2f}.json', rows)
            print(json.dumps({'fraction': fraction, **results[fraction]['local']['regions'],
                              'gong': results[fraction]['gong']}, ensure_ascii=False), flush=True)
        use = resources.sample()
    for endpoint, recorded in ((0., base_report['metrics']), (1., study['results']['mixed_gong']['local']['pixels'])):
        if abs(results[endpoint]['local']['pixels']['dice'] - recorded['dice']) > 1e-5:
            raise ValueError('参数插值端点不能复算已登记 Dice')
    if (results[0.]['gong'] != study['warm_start']['gong']
            or results[1.]['gong'] != study['results']['mixed_gong']['gong']):
        raise ValueError('GONG 开发结果不能复算两个端点')
    checks = {fraction: meets_gate(result, results[0.], results[1.])
              for fraction, result in results.items() if 0 < fraction < 1}
    admitted = all(checks[PRIMARY].values())
    candidate_file = None
    if admitted:
        candidate_file = 'research-alpha-0.50.safetensors'
        state = {k: v.contiguous() for k, v in blend(original, tuned, parameters, PRIMARY).items()}
        commit(output / candidate_file, lambda stream: stream.write(save(state)))
    report = {
        'state': 'completed', 'recorded_utc': now(), 'results': results, 'checks': checks,
        'primary_passed': admitted, 'candidate': candidate_file,
        'candidate_sha256': digest(output / candidate_file) if candidate_file else None,
        'protocol_sha256': digest(output / 'protocol.json'),
        'development_hashes': {f'{a:.2f}': digest(output / f'alpha-{a:.2f}.json') for a in FRACTIONS},
        'resources': use, 'test_accessed': False, 'calibration_accessed': False, 'deployed': False,
    }
    atomic_json(output / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'release', 'later', 'adapted', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    configure_environment()
    report = run(**vars(parser.parse_args()))
    print(json.dumps({key: report[key] for key in ('state', 'checks', 'primary_passed')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
