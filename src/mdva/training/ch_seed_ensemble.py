"""Compare equally weighted CH seed ensembles under a fixed regional protocol."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import label

from .ar_hmi_temporal_ranker import object_scores
from .ch_boundary_growth import CONNECTIVITY, candidate_masks
from .datasets import SegmentationDataset
from .durable import atomic_json
from .fl_gong_resolution import add_isolated
from .journal import read
from .metrics import binary_segmentation, segmentation_summary
from .runtime import digest, now, output_path


MODELS = ('CH-C2-s2026', 'CH-C2-s17', 'CH-C2-s42')
FORMULAS = ('single_seed', 'mean_three', 'median_three', 'isolated_consensus')


def prepare_masks(stack):
    if stack.shape != (len(MODELS), 512, 512) or not np.isfinite(stack).all():
        raise ValueError('冕洞融合要求三个同网格、数值有限的概率图')
    anchor = candidate_masks(stack[0])['bounded_growth']
    average = candidate_masks(stack.mean(axis=0, dtype=np.float32))['bounded_growth']
    consensus = candidate_masks(np.median(stack, axis=0))['bounded_growth']
    return dict(zip(FORMULAS, (anchor, average, consensus, add_isolated(anchor, consensus)), strict=True))


def evaluate(mask, ref, raw):
    components, n = label(mask, structure=CONNECTIVITY)
    areas = np.bincount(components.ravel(), minlength=n + 1)[1:]
    scored, geometry, _ = object_scores(components, [{'area': int(v)} for v in areas], ref, [True] * n)
    matrix = np.asarray(geometry['iou'], dtype=float).reshape(n, len(geometry['reference_ids']))
    scored['no_overlap_references'] = int((matrix.max(axis=0, initial=0) == 0).sum())
    scored['pixels'] = binary_segmentation(mask, raw)
    return scored


def total(rows, model):
    values = [r['models'][model] for r in rows]
    counters = {k:sum(r[k] for r in values) for k in ('reference', 'predicted', 'matched', 'no_overlap_references')}
    n, p, m = (counters[k] for k in ('reference', 'predicted', 'matched'))
    sizes = {}
    for row in values:
        for size, counts in row['by_size'].items():
            sizes.setdefault(size, Counter()).update(counts)
    return {
        **counters, 'images': len(rows), 'unmatched': p - m, 'precision': m / p if p else None,
        'recall': m / n if n else None, 'by_size': sizes, 'pixels': segmentation_summary([r['pixels'] for r in values]),
    }


def intervals(rows):
    times = [int(datetime.fromisoformat(r['time_utc']).timestamp() // (27 * 86400)) for r in rows]
    blocks = np.asarray(times)
    groups = np.unique(blocks)
    sample = np.random.default_rng(2026).integers(0, len(groups), (2000, len(groups)))
    result = {}
    for method in FORMULAS:
        counts = np.asarray([[r['models'][method][k] for k in ('matched', 'predicted', 'reference')] for r in rows])
        block_sums = np.stack([counts[blocks == block].sum(axis=0) for block in groups])
        pooled = block_sums[sample].sum(axis=1)
        result[method] = pooled[:, :1] / pooled[:, 1:]
    return {
        'blocks': len(groups), 'resamples': 2000, 'seed': 2026,
        'compared_to_anchor': {name:dict(zip(('precision_ci95', 'recall_ci95'),
                                            np.quantile(value - result['single_seed'], [.025, .975], axis=0).T.tolist(),
                                            strict=True))
                               for name, value in result.items() if name != 'single_seed'},
    }


def run(dataset, diagnostics, previous, output):
    output = output_path(output, dataset)
    if output.exists() or any(output.is_relative_to(p.resolve()) for p in (diagnostics, previous)):
        raise ValueError('冕洞种子对照须使用新的独立输出目录')
    anchor = read(previous / 'summary.json')
    index = diagnostics / 'case-index.json'
    prior_plan = read(previous / 'protocol.json')
    if (anchor['state'] != 'completed' or anchor['test_accessed'] or anchor['calibration_accessed']
            or read(diagnostics / 'identity.json')['test_accessed']
            or anchor['protocol_sha256'] != digest(previous / 'protocol.json')
            or anchor['images_sha256'] != digest(previous / 'images.json')
            or prior_plan['manifest_sha256'] != digest(dataset / 'manifest-hashes.json')
            or prior_plan['index_sha256'] != digest(index)):
        raise ValueError('早先开发赋值和现有缓存身份不符')
    val = SegmentationDataset(dataset, 'CH', 'validation', 512)
    tables = {m:{} for m in MODELS}
    for row in read(index):
        if row['run_id'] in MODELS and row['diagnostic_subset'] == 'validation':
            table = tables[row['run_id']]
            if row['uri'] in table:
                raise ValueError('重复的模型开发结果')
            table[row['uri']] = row
    original_rows = {r['uri']:r['arms']['bounded_growth'] for r in read(previous / 'images.json')}
    names = {row['uri'] for row in val.rows}
    if len(names) != 526 or set(original_rows) != names or any(set(table) != names for table in tables.values()):
        raise ValueError('各随机种子的开发集图像不同')
    identities = {m:{row['checkpoint_sha256'] for row in table.values()} for m, table in tables.items()}
    if any(len(records) != 1 for records in identities.values()):
        raise ValueError('每个种子应只使用一个固定检查点')
    plan = {
        'code_sha256': digest(__file__), 'previous_sha256': digest(previous / 'summary.json'), 'diagnostic_sha256': digest(index),
        'models': {m:next(iter(records)) for m, records in identities.items()},
        'arms': FORMULAS, 'gate': 'primary mean: recall +3pp and precision loss <=1pp vs single', 'threshold': .5,
        'test_accessed': False, 'calibration_accessed': False,
    }
    atomic_json(output / 'protocol.json', plan)
    rows = []
    for row in val.rows:
        stack = []
        for model, table in tables.items():
            item = table[row['uri']]
            p = diagnostics / 'probabilities' / model / (item['sample_id'] + '.npy')
            if digest(p) != item['probability_sha256'] or row['mask_uri'] != item['mask_uri']:
                raise ValueError('原模型概率缓存未通过身份核验')
            stack.append(np.load(p, allow_pickle=False))
        raw = np.load(val.source(row['mask_uri']), allow_pickle=False) > 0
        refs = label(raw, structure=CONNECTIVITY)[0]
        scored = {m:evaluate(mask, refs, raw) for m, mask in prepare_masks(np.stack(stack)).items()}
        legacy = original_rows[row['uri']]
        current = scored['single_seed']
        if (current['reference'], current['predicted'], current['matched']) != (
            legacy['references'], legacy['predicted'], legacy['matched']
        ):
            raise ValueError('固定单种子区域结果不能精确复算')
        rows.append({'uri': row['uri'], 'time_utc': row['time_utc'], 'models': scored})
    levels = {m:total(rows, m) for m in FORMULAS}
    start, candidate = (levels[m] for m in ('single_seed', 'mean_three'))
    gate = candidate['recall'] >= start['recall'] + .03 and candidate['precision'] >= start['precision'] - .01
    atomic_json(output / 'images.json', rows)
    result = {
        'state': 'completed', 'created_utc': now(), 'arms': levels, 'primary_passed': gate,
        'intervals': intervals(rows), 'protocol_sha256': digest(output / 'protocol.json'),
        'images_sha256': digest(output / 'images.json'), 'test_accessed': False,
        'calibration_accessed': False, 'deployed': False,
    }
    atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset', 'diagnostics', 'previous', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    summary = run(**vars(parser.parse_args()))
    print(json.dumps({k:summary[k] for k in ('state', 'primary_passed', 'arms', 'intervals')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
