"""Score fixed AR checkpoints, small-region recall, and historical event coverage."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path

import numpy as np
import torch
from torch.nn import functional as F

from .ar_scale_study import BUDGET, ROOT, SIZES, configuration, load_plan, warm_model
from .datasets import SegmentationDataset
from .durable import atomic_json
from .forecast_region_detector_audit import match_reference
from .journal import read
from .metrics import binary_segmentation, paired_cluster_interval, segmentation_summary
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


def load_arm(study, side, plan):
    checkpoint = study / str(side) / 'run/last.pt'
    status = read(checkpoint.with_name('status.json'), {})
    if status.get('state') != 'completed' or status.get('completed_epoch') != BUDGET:
        raise ValueError(f'{side} 组未完成固定轮数')
    sha256 = digest(checkpoint)
    saved = load_checkpoint(checkpoint)
    config = configuration(plan, side)
    if (
        saved['epoch'] != BUDGET or saved['identity'] != read(checkpoint.with_name('identity.json'))
        or saved['identity']['config'] != config or saved['identity']['code'] != plan['code']
        or saved['identity']['data'] != plan['data_identity'] or digest(checkpoint) != sha256
    ):
        raise ValueError('检查点或实验身份不一致')
    model, _ = warm_model(plan, config)
    model.load_state_dict(saved['model'])
    return model, sha256, saved['history'][-1]['validation']['dice']


def verified_event_windows(join, validation, manifest_hash):
    summary = read(join / 'summary.json')
    path = join / 'development-joins.jsonl'
    if summary['frozen_manifest_sha256'] != manifest_hash or summary['join_rows_sha256'] != digest(path):
        raise ValueError('历史事件关联与当前开发清单不同')
    by_uri = {item['uri']: item for item in validation.rows}
    windows = defaultdict(list)
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            if row['join_state'] != 'exact_reference_join' or row['split'] != 'validation':
                continue
            uri = row['image_uri']
            if row['detector_split'] != 'validation' or row['mask_uri'] != by_uri.get(uri, {}).get('mask_uri'):
                raise ValueError('历史活动区关联越出当前验证样本')
            windows[uri].append(row)
    return windows, digest(path)


def native_objects(prediction, reference):
    found, count = match_reference(prediction, reference)
    ids, sizes = np.unique(reference[reference > 0], return_counts=True)
    groups = defaultdict(Counter)
    for identity, size in zip(ids, sizes):
        group = 'under256' if size < 256 else ('256to1023' if size < 1024 else '1024plus')
        groups[group].update({'reference': 1, 'matched': int(found[int(identity)] is not None)})
    return {
        'reference': len(found), 'predicted': count, 'matched': sum(value is not None for value in found.values()),
        'by_size': {size: dict(values) for size, values in groups.items()},
        'matched_reference_ids': [identity for identity, value in found.items() if value is not None],
    }


def estimate(rows, arm):
    metrics = [row['arms'][arm] for row in rows]
    refs = sum(row['objects']['reference'] for row in metrics)
    predictions = sum(row['objects']['predicted'] for row in metrics)
    hits = sum(row['objects']['matched'] for row in metrics)
    sizes = defaultdict(Counter)
    for row in metrics:
        for size, counts in row['objects']['by_size'].items():
            sizes[size].update(counts)
    return {
        'pixels': segmentation_summary([row['pixels'] for row in metrics]),
        'reference_regions': refs, 'predicted_regions': predictions, 'matched_regions': hits,
        'false_positive_regions': predictions - hits, 'missed_regions': refs - hits,
        'region_precision': hits / predictions if predictions else 0.,
        'region_recall': hits / refs if refs else None,
        'by_reference_size': {
            key: {**counts, 'recall': counts['matched'] / counts['reference']} for key, counts in sizes.items()
        },
    }


def run(study, output, joined):
    study, joined = Path(study).resolve(), Path(joined).resolve()
    plan = load_plan(study)
    output = output_path(output, plan['dataset'])
    if output.exists() or any(output.is_relative_to(source) for source in (study, joined)):
        raise ValueError('复核需使用新目录')
    data = {size: SegmentationDataset(plan['dataset'], 'AR', 'validation', size) for size in SIZES}
    validation = data[512]
    if len(validation) != 276 or validation.rows != data[1024].rows:
        raise ValueError('两组验证样本集合应一致')
    events, event_sha = verified_event_windows(joined, validation, plan['data_identity']['manifest-hashes.json'])
    sizes = {'warm512': 512, 'trained512': 512, 'trained1024': 1024}
    base, _ = warm_model(plan, configuration(plan, 512))
    arms = {f'trained{side}': load_arm(study, side, plan) for side in SIZES}
    models = {'warm512': base, **{arm: value[0] for arm, value in arms.items()}}
    rows = []
    event_rows = []
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        torch.set_num_threads(3)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
        torch.use_deterministic_algorithms(True)
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        resource = Resources()
        models = {arm: model.cuda().eval() for arm, model in models.items()}
        output.mkdir(parents=True)
        with torch.inference_mode():
            for index, source in enumerate(validation.rows):
                reference = np.load(validation.source(source['mask_uri']), allow_pickle=False)
                inputs = {side: ds[index]['image'][None].cuda() for side, ds in data.items()}
                record = {'uri': source['uri'], 'time': source['time_utc'], 'arms': {}}
                for arm, model in models.items():
                    with torch.autocast('cuda', dtype=torch.float16):
                        logits = model(inputs[sizes[arm]])
                    native = F.interpolate(logits.float().sigmoid(), size=reference.shape,
                                           mode='bilinear', align_corners=False)[0, 0].cpu().numpy()
                    if not np.isfinite(native).all():
                        raise ValueError('复核出现非有限输出')
                    prediction = native >= .5
                    record['arms'][arm] = {
                        'pixels': binary_segmentation(prediction, reference > 0),
                        'objects': native_objects(prediction, reference),
                    }
                for event in events[source['uri']]:
                    instance = event['reference_instance']
                    if not np.any(reference == instance):
                        raise ValueError('历史关联指向当前掩膜中不存在的实例')
                    event_rows.append({
                        'sample_id': event['sample_id'], 'target': event['target'],
                        'noaa': event['noaa'], 'matches': {
                            arm: instance in features['objects']['matched_reference_ids']
                            for arm, features in record['arms'].items()
                        },
                    })
                rows.append(record)
                if (index + 1) % 16 == 0 or index + 1 == len(validation):
                    atomic_json(output / 'progress.json', {'done': index + 1, 'total': len(validation),
                                                           'resources': resource.sample()})
        measured = resource.sample()

    aggregate = {arm: estimate(rows, arm) for arm in sizes}
    replay = {arm: aggregate[arm]['pixels']['dice'] - value[2] for arm, value in arms.items()}
    baseline, control, candidate = (aggregate[key] for key in sizes)
    limits = plan['decision']
    small_delta = (candidate['by_reference_size']['under256']['recall']
                   - control['by_reference_size']['under256']['recall'])
    precision_delta = candidate['region_precision'] - control['region_precision']
    recall_delta = candidate['region_recall'] - control['region_recall']
    dice_delta = candidate['pixels']['dice'] - control['pixels']['dice']
    checks = {
        'checkpoint_replay': all(abs(value) <= 1e-5 for value in replay.values()),
        'small_recall': small_delta >= limits['small_recall_gain'],
        'region_precision': precision_delta >= -limits['overall_precision_drop'],
        'region_recall': recall_delta >= -limits['overall_recall_drop'],
        'dice': dice_delta >= -limits['dice_drop'],
    }
    event_counts = Counter(str(row['target']) for row in event_rows)
    event_coverage = {
        arm: {target: sum(str(row['target']) == target and row['matches'][arm] for row in event_rows)
              for target in event_counts}
        for arm in sizes
    }
    groups = [int(datetime.fromisoformat(row['time']).timestamp() // (86400 * 27)) for row in rows]
    dice = [[row['arms'][arm]['pixels']['dice'] for row in rows] for arm in ('trained512', 'trained1024')]
    atomic_json(output / 'images.json', rows)
    atomic_json(output / 'event-window-coverage.json', event_rows)
    result = {
        'state': 'completed' if checks['checkpoint_replay'] else 'metric_mismatch', 'created_utc': now(),
        'images': len(rows), 'threshold': .5, 'results': aggregate, 'fixed_epoch': BUDGET, 'resources': measured,
        'vs_control': {'small_recall': small_delta, 'region_precision': precision_delta,
                       'region_recall': recall_delta, 'dice': dice_delta},
        'vs_warm_start': {'region_precision': candidate['region_precision'] - baseline['region_precision'],
                          'region_recall': candidate['region_recall'] - baseline['region_recall'],
                          'dice': candidate['pixels']['dice'] - baseline['pixels']['dice']},
        'dice_paired_interval': paired_cluster_interval(*dice, groups),
        'checks': checks, 'practical_gate_passed': all(checks.values()),
        'eighty_percent_object_gate': all(candidate[key] >= .8 for key in ('region_precision', 'region_recall')),
        'replay_delta': replay, 'source_weights_sha256': plan['weights_sha256'],
        'checkpoint_sha256': {arm: value[1] for arm, value in arms.items()},
        'event_windows': dict(event_counts), 'event_window_detector_coverage': event_coverage,
        'event_scope': 'Reference-based development audit only; no operational NOAA assignment or flare forecast.',
        'protocol_sha256': digest(study / 'protocol.json'), 'event_join_sha256': event_sha,
        'code_sha256': digest(__file__), 'images_sha256': digest(output / 'images.json'),
        'event_rows_sha256': digest(output / 'event-window-coverage.json'),
        'test_accessed': False, 'calibration_accessed': False, 'weights_replaced': False,
    }
    atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('study', 'output', 'joined'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    configure_environment()
    result = run(**vars(args))
    print(json.dumps({key: result[key] for key in ('state', 'vs_control', 'checks', 'event_window_detector_coverage')}))


if __name__ == '__main__':
    main()
