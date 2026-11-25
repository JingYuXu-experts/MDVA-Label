"""Score both FL contrast arms at the fixed final epoch on identical images."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import label

from .datasets import SegmentationDataset
from .diagnostic_support import connected_scores
from .durable import atomic_json
from .fl_contrast_study import ARMS, BUDGET, architecture, identity
from .fl_pilot_review import region_summary
from .journal import read
from .metrics import binary_segmentation, paired_cluster_interval, segmentation_summary
from .reviewed_train import data_identity, load_checkpoint
from .runtime import Resources, configure_environment, digest, now, output_path


def load_arm(study, arm, plan):
    run = study / arm / 'run'
    status = read(run / 'status.json', {})
    if status.get('state') != 'completed' or status.get('completed_epoch') != BUDGET:
        raise ValueError(f'{arm} 尚无完整的固定第{BUDGET}轮')
    path = run / 'last.pt'
    sha256 = digest(path)
    saved = load_checkpoint(path)
    config = {**plan['config'], 'id': f'FL-local-contrast-{arm}-s2026', 'arm': arm}
    archived = saved['identity']
    if (
        digest(path) != sha256 or saved['epoch'] != BUDGET
        or archived != read(run / 'identity.json')
        or archived['config'] != config or archived['code'] != plan['code']
        or archived['data'] != plan['data_identity']
    ):
        raise ValueError('配对检查点身份不符')
    model, _ = architecture(config)
    model.load_state_dict(saved['model'])
    return model.eval(), sha256, saved['history'][-1]['validation']['dice']


def statistics(rows, arm):
    return {
        'pixel_metrics': segmentation_summary([row['arms'][arm]['pixels'] for row in rows]),
        'regions': region_summary([row['arms'][arm]['regions'] for row in rows]),
    }


def run(study, output):
    import torch

    study = Path(study).resolve()
    plan_file = study / 'protocol.json'
    plan = read(plan_file)
    dataset = plan['dataset']
    output = output_path(output, dataset)
    if output.exists() or output.is_relative_to(study):
        raise ValueError('复核使用独立新输出目录')
    if plan['code'] != identity() or plan['data_identity'] != data_identity(dataset):
        raise ValueError('输入或训练协议代码改变')
    starting_report = Path(plan['config']['warm_start_weights']).parent / 'summary.json'
    if digest(starting_report) != plan['initial_review_sha256']:
        raise ValueError('起点的开发验证记录改变')
    start = read(starting_report)
    torch.set_num_threads(2)
    arms = {arm: load_arm(study, arm, plan) for arm in ARMS}
    data = SegmentationDataset(dataset, 'FL', 'validation', 512)
    if len(data) != 182:
        raise ValueError('缺少完整开发验证图像')
    output.mkdir(parents=True)
    resource = Resources('cpu')
    rows = []
    with torch.inference_mode():
        for index, row in enumerate(data.rows):
            x = data[index]['image'][None]
            reference = np.load(data.source(row['mask_uri']), allow_pickle=False) > 0
            labels = label(reference, structure=np.ones((3, 3)))[0]
            detail = {'uri': row['uri'], 'station': row['instrument'], 'time_utc': row['time_utc'], 'arms': {}}
            for arm, (model, _, _) in arms.items():
                logits = model(x)
                if not torch.isfinite(logits).all():
                    raise ValueError('验证输出含非有限值')
                prediction = logits.sigmoid()[0, 0].numpy() >= .5
                detail['arms'][arm] = {
                    'pixels': binary_segmentation(prediction, reference, boundary=True),
                    'regions': connected_scores(prediction, labels),
                }
            rows.append(detail)
            resource.sample()
            if (index + 1) % 25 == 0 or index + 1 == len(data):
                atomic_json(output / 'progress.json', {'images_complete': index + 1, 'total': len(data)})
    final = {arm: statistics(rows, arm) for arm in ARMS}
    replay = {arm: final[arm]['pixel_metrics']['dice'] - values[2] for arm, values in arms.items()}
    stations = sorted({row['station'] for row in rows})
    grouped = {
        station: {arm: statistics([row for row in rows if row['station'] == station], arm) for arm in ARMS}
        for station in stations
    }
    original, candidate = [final[arm]['regions'] for arm in ARMS]
    recall_gain = candidate['recall'] - original['recall']
    precision_gain = candidate['precision'] - original['precision']
    station_gains = {
        station: stats['contrast']['pixel_metrics']['dice'] - stats['control']['pixel_metrics']['dice']
        for station, stats in grouped.items()
    }
    blocks = [int(datetime.fromisoformat(row['time_utc']).timestamp() // (86400 * 27)) for row in rows]
    dice = [[row['arms'][arm]['pixels']['dice'] for row in rows] for arm in ARMS]
    image_path = output / 'images.json'
    atomic_json(image_path, rows)
    replay_ok = all(abs(delta) <= 1e-5 for delta in replay.values())
    checks = {
        'checkpoint_replay': replay_ok, 'region_recall_gain_at_least_3pp': recall_gain >= .03,
        'region_precision_loss_at_most_1pp': precision_gain >= -.01,
        'station_dice_drop_at_most_3pp': min(station_gains.values()) >= -.03,
    }
    result = {
        'state': 'completed' if replay_ok else 'metric_mismatch', 'created_utc': now(), 'epoch': BUDGET,
        'threshold': .5, 'validation_images': len(rows), 'results': final, 'stations': grouped,
        'checkpoint_sha256': {arm: values[1] for arm, values in arms.items()},
        'warm_start': {'pixel_metrics': start['metrics'], 'regions': start['regions']},
        'dice_change_from_warm_start': {
            arm: stats['pixel_metrics']['dice'] - start['metrics']['dice'] for arm, stats in final.items()
        },
        'recall_gain': recall_gain, 'precision_gain': precision_gain, 'station_dice_gains': station_gains,
        'dice_paired_27d_interval': paired_cluster_interval(*dice, blocks),
        'cpu_minus_gpu_dice': replay, 'checks': checks, 'engineering_gate_passed': all(checks.values()),
        'protocol_sha256': digest(plan_file), 'images_sha256': digest(image_path),
        'code_sha256': digest(__file__), 'resources': resource.sample(),
        'scope': 'Same validation set, single paired seed. Passing the engineering gate merits further validation.',
        'test_accessed': False, 'weights_replaced': False,
    }
    atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    configure_environment()
    result = run(args.study, args.output)
    print(json.dumps({key: result[key] for key in ('state', 'recall_gain', 'precision_gain', 'checks')}))


if __name__ == '__main__':
    main()
