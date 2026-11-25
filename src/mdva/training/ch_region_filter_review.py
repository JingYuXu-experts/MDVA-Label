"""Compare fixed region size filters on matching CH development predictions."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import label
from scipy.optimize import linear_sum_assignment

from .datasets import SegmentationDataset
from .diagnostic_support import sample_id
from .durable import atomic_json
from .metrics import binary_segmentation
from .runtime import configure_environment, digest, now, output_path


FLOORS = (1, 8, 32, 128)
CONNECTIVITY = np.ones((3, 3), dtype=bool)


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def load_prediction(directory, identity, case_id, uri, filename, score_key):
    case = directory / 'cases' / case_id
    result = read_json(case / 'result.json')
    asset = case / filename
    if result['uri'] != uri or result['identity_sha256'] != identity or digest(asset) != result['assets'][filename]:
        raise ValueError('冕洞缓存或逐图评价校验失败')
    with Image.open(asset) as image:
        prediction = np.asarray(image) > 0
    return prediction, result['scores']['original'] if score_key == 'original' else result['score']


def region_replay(prediction, reference, historical):
    if prediction.shape != reference.shape:
        raise ValueError('缓存掩膜尺寸不一致')
    foreground = reference > 0
    baseline = binary_segmentation(prediction, foreground)
    if any(baseline[k] != historical[k] for k in ('tp', 'fp', 'fn')):
        raise ValueError('重新读出的像素计数与历史评价不一致')

    components, n_predicted = label(prediction, structure=CONNECTIVITY)
    n_reference = int(reference.max())
    pred_areas = np.bincount(components.ravel(), minlength=n_predicted + 1)
    ref_areas = np.bincount(reference.ravel(), minlength=n_reference + 1)[1:]
    overlaps = np.zeros((n_predicted, n_reference), dtype=np.int64)
    for idx in range(n_reference):
        overlaps[:, idx] = np.bincount(components[reference == idx + 1], minlength=n_predicted + 1)[1:]
    unions = pred_areas[1:, None] + ref_areas[None, :] - overlaps
    ious = np.divide(overlaps, unions, out=np.zeros_like(unions, dtype=float), where=unions > 0)
    for floor in FLOORS:
        keep = pred_areas[1:] >= floor
        filtered_ious = ious[keep]
        assigned_pred, assigned_ref = linear_sum_assignment(-filtered_ious)
        matched = set(assigned_ref[filtered_ious[assigned_pred, assigned_ref] >= .5].tolist())
        kept = np.r_[False, keep]
        pixels = binary_segmentation(kept[components], foreground)
        yield floor, matched, {
            'references': n_reference, 'predicted': int(keep.sum()),
            'matched': len(matched), 'dice': pixels['dice'],
            'reference_pixels_removed': baseline['tp'] - pixels['tp'],
            'false_positive_pixels_removed': baseline['fp'] - pixels['fp'],
        }


def aggregate(rows):
    counts = {key: sum(row[key] for row in rows) for key in (
        'references', 'predicted', 'matched', 'gained_matches', 'lost_matches',
        'reference_pixels_removed', 'false_positive_pixels_removed',
    )}
    return {
        'images': len(rows), **counts, 'dice': float(np.mean([row['dice'] for row in rows])),
        'precision': counts['matched'] / counts['predicted'] if counts['predicted'] else None,
        'recall': counts['matched'] / counts['references'] if counts['references'] else None,
    }


def run(dataset, original, scss, previous, output):
    dataset, original, scss, previous = [Path(path).resolve() for path in (dataset, original, scss, previous)]
    output = output_path(output, dataset)
    if output.exists() or any(output.is_relative_to(path) for path in (original, scss, previous)):
        raise ValueError('冕洞比较必须使用新目录')
    prior = read_json(previous / 'summary.json')
    scss_summary = read_json(scss / 'summary.json')
    if (
        prior['state'] != 'completed' or prior['test_accessed']
        or prior['protocol_sha256'] != digest(previous / 'protocol.json')
        or scss_summary['state'] != 'completed' or scss_summary['test_accessed']
        or not scss_summary['exact_replay']
        or scss_summary['identity'] != read_json(scss / 'identity.json')
    ):
        raise ValueError('只允许已复算且仅使用验证集的来源')
    models = {
        'CH-C2-s2026': (original, 'original.png', 'original'),
        'SCSS-best': (scss, 'prediction.png', 'candidate'),
    }
    identities = {model: digest(directory / 'identity.json') for model, (directory, _, _) in models.items()}
    data = SegmentationDataset(dataset, 'CH', 'validation')
    grouped = defaultdict(list)
    rows = []
    complement = defaultdict(lambda: defaultdict(int))
    for row in data.rows:
        uri = row['uri']
        mask = np.load(data.source(row['mask_uri']), allow_pickle=False)
        reference, _ = label(mask > 0, structure=CONNECTIVITY)
        year = row['time_utc'][:4]
        result = {'uri': uri, 'year': year, 'models': {}}
        paired = defaultdict(dict)
        for model, (directory, filename, score_key) in models.items():
            prediction, historical = load_prediction(directory, identities[model], sample_id(uri), uri, filename, score_key)
            model_result = {}
            for floor, found, scores in region_replay(prediction, reference, historical):
                paired[floor][model] = found
                raw = paired[1][model]
                scores['gained_matches'] = len(found - raw)
                scores['lost_matches'] = len(raw - found)
                model_result[floor] = scores
                grouped[(model, floor, 'all')].append(scores)
                grouped[(model, floor, year)].append(scores)
            result['models'][model] = model_result
        for floor, matches in paired.items():
            old, new = matches.values()
            counts = complement[floor]
            counts['reference'] += int(reference.max())
            counts['both'] += len(old & new)
            counts['original_only'] += len(old - new)
            counts['scss_only'] += len(new - old)
            counts['neither'] += int(reference.max()) - len(old | new)
        rows.append(result)

    results = {f'{model}/{floor}/{year}': aggregate(group) for (model, floor, year), group in sorted(grouped.items())}
    for floor in FLOORS:
        current = results[f'CH-C2-s2026/{floor}/all']
        old = prior['results'][str(floor)]
        if (
            any(current[k] != old['regions'][k] for k in ('predicted', 'matched'))
            or current['references'] != old['regions']['reference']
            or abs(current['dice'] - old['metrics']['dice']) > 1e-12
        ):
            raise ValueError(f'旧候选的区域结果无法复算：{floor}')
    if abs(results['SCSS-best/1/all']['dice'] - scss_summary['metrics']['dice']) > 1e-12:
        raise ValueError('SCSS 对照的 Dice 无法复算')

    output.mkdir(parents=True)
    atomic_json(output / 'images.json', rows)
    summary = {
        'state': 'completed', 'created_utc': now(), 'images': len(rows), 'results': results,
        'scss_checkpoint_epoch': scss_summary['checkpoint_epoch'],
        'complementarity': {floor: dict(counts) for floor, counts in complement.items()},
        'scope': 'CH development only. 8-connected region matches at mask IoU >= 0.5. Revision comparison uses reference identities only for scoring.',
        'interpretation': 'Complementarity describes an oracle union of successful matches. It is not a validated ensemble policy.',
        'identities': identities, 'code_sha256': digest(__file__),
        'manifest_sha256': digest(dataset / 'manifest-hashes.json'),
        'original_filter_sha256': digest(previous / 'summary.json'),
        'scss_summary_sha256': digest(scss / 'summary.json'), 'rows_sha256': digest(output / 'images.json'),
        'test_accessed': False, 'calibration_accessed': False, 'production_policy_changed': False,
    }
    atomic_json(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset', 'original', 'scss', 'previous', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    configure_environment()
    summary = run(**vars(args))
    print(json.dumps({
        'images': summary['images'], 'complementarity': summary['complementarity'],
        'results': {k: v for k, v in summary['results'].items() if k.endswith('/all')},
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
