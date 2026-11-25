"""Evaluate a fixed equal-weight ensemble of two archived filament segmenters."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import label
from scipy.optimize import linear_sum_assignment
import torch

from .datasets import SegmentationDataset
from .diagnostic_support import connected_scores
from .durable import atomic_json
from .fl_author_model import AuthorGraph
from .fl_pilot_review import region_summary
from .journal import read
from .metrics import binary_segmentation, segmentation_summary
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


ROOT = Path(__file__).resolve().parents[3]
DATASET = ROOT / 'out/pretraining-20261112/dataset-ready'
DIAGNOSTIC = ROOT / 'out/model-diagnostics/20261116-final'
PILOT = ROOT / 'out/recognition-improvement-20261118/fl-full-pilot-epoch03-review'
ARMS = ('original_unet', 'author_unet', 'equal_weight')
CONNECTIVITY = np.ones((3, 3), dtype=bool)


def overlap_profile(mask, reference):
    predicted, n_predicted = label(mask, structure=CONNECTIVITY)
    n_reference = int(reference.max())
    paired = np.bincount(
        (predicted * (n_reference + 1) + reference).ravel(),
        minlength=(n_predicted + 1) * (n_reference + 1),
    ).reshape(n_predicted + 1, n_reference + 1)
    intersections = paired[1:, 1:]
    union = paired[1:, :].sum(axis=1)[:, None] + paired[:, 1:].sum(axis=0)[None, :] - intersections
    iou = np.divide(intersections, union, out=np.zeros_like(union, dtype=float), where=union > 0)
    a, b = linear_sum_assignment(-iou)
    values = iou[a, b]
    return {
        'zero_overlap_references': int(np.count_nonzero(~intersections.any(axis=0))),
        'matched_iou_030': int((values >= .3).sum()),
        'matched_iou_050': int((values >= .5).sum()),
    }


def run(output):
    output = output_path(output, DATASET)
    if output.exists() or any(output.is_relative_to(p) for p in (DIAGNOSTIC, PILOT)):
        raise ValueError('暗条集成复核须使用新的独立输出目录')
    index = DIAGNOSTIC / 'case-index.json'
    pilot = read(PILOT / 'summary.json')
    checkpoint = PILOT / 'checkpoint.pt'
    if (pilot['state'] != 'completed' or pilot['test_accessed'] or pilot['calibration_accessed']
            or read(DIAGNOSTIC / 'identity.json')['test_accessed']
            or pilot['checkpoint_sha256'] != digest(checkpoint)
            or pilot['baseline_sha256'] != digest(index)):
        raise ValueError('暗条集成输入身份不符')
    baseline = {
        row['uri']: row for row in read(index)
        if row['run_id'] == 'FL-F0-s2026' and row['diagnostic_subset'] == 'validation'
    }
    data = SegmentationDataset(DATASET, 'FL', 'validation', 512)
    if len(data) != 182 or set(baseline) != {row['uri'] for row in data.rows}:
        raise ValueError('暗条开发验证清单不一致')
    saved = load_checkpoint(checkpoint)
    if saved['epoch'] != 3 or saved['epoch'] != pilot['epoch']:
        raise ValueError('暗条对照检查点轮次不符')
    model = AuthorGraph('batch')
    model.load_state_dict(saved['model'])
    protocol = {
        'split': 'validation', 'pilot_sha256': digest(checkpoint), 'validation_index_sha256': digest(index),
        'source_sha256': digest(__file__), 'weights': [.5, .5], 'mask_threshold': .5,
        'strict_gate': {'recall_gain': .03, 'allowed_precision_loss': .01},
        'test_accessed': False, 'calibration_accessed': False,
    }
    atomic_json(output / 'protocol.json', protocol)
    rows = []
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cuda.matmul.allow_tf32 = False
        model.cuda().eval()
        resource = Resources()
        with torch.inference_mode():
            for i, row in enumerate(data.rows):
                entry = baseline[row['uri']]
                prob_path = DIAGNOSTIC / 'probabilities/FL-F0-s2026' / (entry['sample_id'] + '.npy')
                if entry['mask_uri'] != row['mask_uri'] or entry['probability_sha256'] != digest(prob_path):
                    raise ValueError('暗条概率缓存和参考图不匹配')
                p = np.load(prob_path, allow_pickle=False).astype(np.float32)
                q = model(data[i]['image'][None].cuda()).sigmoid()[0, 0].cpu().numpy()
                ref = np.load(data.source(row['mask_uri']), allow_pickle=False) > 0
                if not (p.shape == q.shape == ref.shape and np.isfinite(p).all() and np.isfinite(q).all()):
                    raise ValueError('暗条图像、概率或掩膜无法配对')
                labels = label(ref, structure=CONNECTIVITY)[0]
                results = {}
                for arm, probability in zip(ARMS, (p, q, (p + q) / 2), strict=True):
                    mask = probability >= .5
                    metrics = binary_segmentation(mask, ref)
                    regions = connected_scores(mask, labels)
                    overlap = overlap_profile(mask, labels)
                    if overlap['matched_iou_050'] != regions['matched']:
                        raise ValueError('轮廓重合复算与原评价口径不一致')
                    results[arm] = {'pixel': metrics, 'regions': regions, 'overlap': overlap}
                rows.append({'uri': row['uri'], 'station': row['instrument'],
                             'time_utc': row['time_utc'], 'arms': results})
                if i % 24 == 0:
                    resource.sample()
        use = resource.sample()
    scores = {}
    for arm in ARMS:
        cases = [row['arms'][arm] for row in rows]
        scores[arm] = {
            'pixel': segmentation_summary([case['pixel'] for case in cases]),
            'regions': region_summary([case['regions'] for case in cases]),
            'overlap': {key: sum(case['overlap'][key] for case in cases) for key in cases[0]['overlap']},
        }
    for arm, name in zip(ARMS[:2], ('baseline_metrics', 'metrics'), strict=True):
        if abs(scores[arm]['pixel']['dice'] - pilot[name]['dice']) > 1e-5:
            raise ValueError('暗条单模型回放与已核验的 Dice 不一致')
    control, combined = (scores[name]['regions'] for name in ARMS[1:])
    passed = combined['recall'] >= control['recall'] + .03 and combined['precision'] >= control['precision'] - .01
    atomic_json(output / 'images.json', rows)
    result = {
        'state': 'completed', 'created_utc': now(), 'images': len(rows), 'metrics': scores,
        'gate_passed': passed, 'protocol_sha256': digest(output / 'protocol.json'),
        'images_sha256': digest(output / 'images.json'), 'resources': use,
        'test_accessed': False, 'calibration_accessed': False, 'deployed': False,
    }
    atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    configure_environment()
    report = run(parser.parse_args().output)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
