"""Measure incremental flare ranking from texture inside predicted AR masks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import maximum_filter, minimum_filter
import torch
from torch.nn import functional as F

from .ar_scale_study import ROOT, configuration, load_plan, warm_model
from .datasets import SegmentationDataset
from .durable import atomic_json
from .forecast_disk_baseline import CORE, RAW, REGIONS, features
from .forecast_disk_xrs import XRS, operating_point, scores, train_fit
from .journal import append, read
from .metrics import forecast_scores
from .reviewed_train import seed_all
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path
from .xrs_coverage import utc


TEXTURE = ('display_opposite_tone_fraction', 'display_opposite_tone_gradient')
MASKED = tuple('ar_' + key for key in TEXTURE)
ARMS = {'baseline': (*XRS, *RAW), 'disk_texture': (*XRS, *RAW, *TEXTURE),
        'disk_and_ar_texture': (*XRS, *RAW, *TEXTURE, *MASKED)}


def texture(gray, core, candidates):
    if gray.shape != core.shape or core.shape != candidates.shape or not core.any() or gray.dtype != np.uint8:
        raise ValueError('灰度图、日面范围和预测区域需使用同一网格')
    midpoint = np.median(gray[core])
    high = maximum_filter(gray, size=9, mode='nearest') >= midpoint + 16
    low = minimum_filter(gray, size=9, mode='nearest') <= midpoint - 16
    full_support = high & low & core
    values = gray.astype(np.float32)
    dy, dx = np.gradient(values)
    gradient = np.hypot(dx, dy) / 255
    scale = int(core.sum())
    output = {}
    for names, support in ((TEXTURE, full_support), (MASKED, full_support & candidates)):
        output[names[0]] = int(support.sum()) / scale
        output[names[1]] = float(gradient[support].sum(dtype=np.float64)) / scale
    return output


def setup(study, previous, output):
    study, previous = (Path(path).resolve() for path in (study, previous))
    data = load_plan(study)
    output = output_path(output, data['dataset'])
    if any(output.is_relative_to(path) for path in (study, previous)):
        raise ValueError('预报特征需要独立目录')
    report = read(previous / 'summary.json')
    if (report['state'] != 'completed' or report['test_accessed'] or report['calibration_accessed']
            or report['rows_sha256'] != digest(previous / 'rows.json')):
        raise ValueError('历史预报输入身份不符')
    rows = read(previous / 'rows.json')
    if {r['split'] for r in rows} != {'train', 'validation'}:
        raise ValueError('仅接受训练与开发资料')
    datasets = {split: SegmentationDataset(data['dataset'], 'AR', split) for split in ('train', 'validation')}
    index = {split: {row['uri']: row for row in source.rows} for split, source in datasets.items()}
    for row in rows:
        candidate = index[row['split']].get(row['uri'])
        if (not candidate or utc(candidate['time_utc']) != utc(row['observed_utc'])
                or datasets[row['split']].fingerprints[row['uri']]['sha256'] != row['source_sha256']):
            raise ValueError('图像指纹、时间或划分不符')
    protocol = {
        'weights_sha256': data['weights_sha256'], 'study_sha256': digest(study / 'protocol.json'),
        'previous_report_sha256': digest(previous / 'summary.json'), 'rows_sha256': report['rows_sha256'],
        'code_sha256': digest(__file__), 'arms': {name: list(keys) for name, keys in ARMS.items()},
        'design': 'Same fixed windows, predicted masks only, comparable image-intensity gradients with and without masks.',
        'scope': 'JPEG display intensity. No physical field units or inferred magnetic energy.',
        'test_accessed': False, 'calibration_accessed': False,
    }
    output.mkdir(parents=True, exist_ok=True)
    protocol_path = output / 'protocol.json'
    if protocol_path.exists() and read(protocol_path) != protocol:
        raise ValueError('特征保存目录的协议已变化')
    if not protocol_path.exists():
        atomic_json(protocol_path, protocol)
    return output, data, rows, datasets, report, digest(protocol_path)


def extract(output, data, rows, datasets, protocol_hash):
    cached = output / 'per-window'
    cached.mkdir(exist_ok=True)
    config = configuration(data, 512)
    model, _ = warm_model(data, config)
    result = []
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        seed_all(config['seed'])
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        resource = Resources()
        model.cuda().eval()
        with torch.inference_mode():
            for number, row in enumerate(rows, 1):
                if (output / 'PAUSE').exists():
                    return None
                path = cached / f'{number:05}.json'
                items = read(path)
                if items:
                    if (items['protocol_sha256'], items['source_sha256']) != (protocol_hash, row['source_sha256']):
                        raise ValueError('特征缓存不对应当前图像和协议')
                    result.append({**row, 'features': {**row['features'], **items['features']}})
                    continue
                source = datasets[row['split']]
                with Image.open(source.source(row['uri'])) as file:
                    image = file.convert('L')
                gray = np.asarray(image)
                if gray.shape != CORE.shape:
                    raise ValueError('显示域观测网格偏离已有产品')
                resized = np.array(image.resize((512, 512), Image.Resampling.BILINEAR), dtype=np.float32) / 255
                x = torch.from_numpy(resized)[None, None].cuda()
                with torch.autocast('cuda', dtype=torch.float16):
                    logits = model(x)
                if not torch.isfinite(logits).all():
                    raise ValueError('固定范围检测器输出非有限值')
                probabilities = F.interpolate(logits.float().sigmoid(), size=gray.shape, mode='bilinear',
                                              align_corners=False)[0, 0].cpu().numpy()
                mask = probabilities >= .5
                original = features(gray, mask)
                if any(abs(original[key] - row['features'][key]) > 1e-7 for key in (*RAW, *REGIONS)):
                    raise ValueError(f'原磁图与区域对照未复算：{row["uri"]}')
                values = texture(gray, CORE, mask)
                atomic_json(path, {'protocol_sha256': protocol_hash, 'source_sha256': row['source_sha256'],
                                    'features': values})
                result.append({**row, 'features': {**row['features'], **values}})
                if number % 64 == 0:
                    atomic_json(output / 'progress.json', {'state': 'extracting', 'windows': number,
                                                            'total': len(rows), 'resource': resource.sample()})
        measured = resource.sample()
    return result, measured


def evaluate(rows, report):
    from sklearn.metrics import average_precision_score

    training = [row for row in rows if row['split'] == 'train']
    validation = [row for row in rows if row['split'] == 'validation']
    if max(utc(row['target_end_utc']) for row in training) >= min(utc(row['observed_utc']) for row in validation):
        raise ValueError('训练窗越过开发分区')
    truth = np.asarray([row['target'] for row in validation])
    climate = float(np.mean([row['target'] for row in training]))
    results, predictions = {}, {}
    for name, keys in ARMS.items():
        prediction = scores(train_fit(training, keys), validation, keys)
        predictions[name] = prediction
        policy = operating_point(training, keys)
        if policy is None:
            raise ValueError('未能按原训练滚动折计算阈值')
        results[name] = {**policy, 'metrics': forecast_scores(truth, prediction, threshold=policy['threshold'], climate=climate)}
    prior = report['training_only_operating_points']['xrs_plus_raw']['development_metrics']
    if any(abs(prior[key] - results['baseline']['metrics'][key]) > 1e-8 for key in ('pr_auc_ap', 'brier', 'tp', 'fp')):
        raise ValueError('已有同样本预报基线未复现')
    blocks = np.array([int(utc(row['observed_utc']).timestamp() // (27*86400)) for row in validation])
    groups = [np.flatnonzero(blocks == block) for block in np.unique(blocks)]
    rng = np.random.default_rng(2026)
    improvements = []
    for _ in range(2000):
        selection = np.concatenate([groups[index] for index in rng.integers(len(groups), size=len(groups))])
        target = truth[selection]
        if len(np.unique(target)) != 2:
            continue
        base, disk, masked = (average_precision_score(target, predictions[name][selection]) for name in ARMS)
        improvements.append([disk-base, masked-disk])
    interval = np.quantile(improvements, [.025, .975], axis=0).T.tolist()
    return results, interval, len(validation), int(truth.sum())


def run(study, previous, output):
    output, data, rows, datasets, report, fingerprint = setup(study, previous, output)
    computed = extract(output, data, rows, datasets, fingerprint)
    if computed is None:
        return {'state': 'paused'}
    rows, resource = computed
    evaluation, intervals, windows, positives = evaluate(rows, report)
    atomic_json(output / 'features.json', rows)
    result = {'state': 'completed', 'created_utc': now(), 'windows': windows, 'positives': positives,
              'results': evaluation, 'paired_ap_ci95': dict(zip(('add_disk_texture', 'add_ar_texture'), intervals)),
              'protocol_sha256': fingerprint, 'features_sha256': digest(output / 'features.json'), 'resource': resource,
              'forecast_released': False, 'test_accessed': False, 'calibration_accessed': False}
    atomic_json(output / 'summary.json', result)
    atomic_json(output / 'progress.json', {'state': 'completed', 'windows': len(rows), 'total': len(rows)})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('study', 'previous', 'output'):
        parser.add_argument('--' + key, required=True)
    args = parser.parse_args()
    configure_environment()
    append(ROOT / 'out/training-20261112-v2', 'development', '耀斑预报活动区范围增量复核',
           '在历史窗口对照全日面纹理、预测区域内纹理及已保存的GOES基线。')
    result = run(**vars(args))
    print(json.dumps({key: result[key] for key in ('state', 'results', 'paired_ap_ci95') if key in result}))


if __name__ == '__main__':
    main()
