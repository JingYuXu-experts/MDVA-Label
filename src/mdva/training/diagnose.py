"""Read-only model diagnosis. No training, test unlock or package publication."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import argparse
import gc
import hashlib
import json
from pathlib import Path
import shutil
import time

import numpy as np
from PIL import Image

from .diagnostic_support import (SCHEMA, allowed_rows, cache_ready, comparisons, read, sample_id,
                                 score, select_samples, stratify, summarize, write)
from .runtime import digest, exclusive, now, output_path
from .run_locations import run_folder, model_and_identity

ROOT = Path(__file__).resolve().parents[3]


def status(output, phase, message, **extra):
    write(output/'status.json', {'state': 'running', 'phase': phase, 'message': message,
          'updated_utc': now(), 'task_type': 'diagnostic_inference', 'training_stopped': True, **extra})


def ensure_stopped(experiment):
    from .continuation import lock_alive
    if not (experiment/'PAUSE').is_file():
        raise ValueError('诊断要求先显式暂停训练，不能自行解除暂停')
    if any(lock_alive(experiment/(name+'.lock')) is not False for name in ('continuation', 'queue', 'watchdog')):
        raise ValueError('训练、队列或守护仍运行或无法核验；诊断拒绝启动')


def png(path, array):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp.png')
    Image.fromarray(array).save(temporary)
    temporary.replace(path)


def audit(dataset, output, rows):
    from .datasets import read_frozen, resolve_uri
    sources = read(dataset/'sources.json')
    fingerprints = {r['uri']: r for r in read_frozen(dataset, 'source-files.jsonl')}
    previous = read(output/'audit.json')
    if previous:
        # Cached scores must not hide modified raw inputs after a restart.
        for index, row in enumerate(rows):
            for key in ('uri', 'mask_uri'):
                path = resolve_uri(row[key], sources)
                expected = fingerprints[row[key]]
                st = path.stat()
                if (st.st_size, st.st_mtime_ns) != (expected['bytes'], expected['mtime_ns']) or digest(path) != expected['sha256']:
                    raise ValueError('缓存复核发现源文件变化：'+row[key])
            if index % 100 == 0:
                status(output, 'D1', '复核缓存对应的原始文件指纹', processed=index+1, total=len(rows))
        return previous
    items, issues = [], []
    grouped = defaultdict(Counter)
    for index, row in enumerate(rows):
        if row['split'] not in ('train', 'validation'):
            raise ValueError('禁止读取测试或校准图像')
        paths = [resolve_uri(row[k], sources) for k in ('uri', 'mask_uri')]
        for key, path in zip(('uri', 'mask_uri'), paths):
            expected = fingerprints[row[key]]
            st = path.stat()
            if (st.st_size, st.st_mtime_ns) != (expected['bytes'], expected['mtime_ns']) or digest(path) != expected['sha256']:
                raise ValueError('源文件身份不符：'+row[key])
        with Image.open(paths[0]) as image:
            rgb = np.asarray(image.convert('RGB'))
            gray = np.asarray(image.convert('L'))
        mask = np.load(paths[1], allow_pickle=False)
        if mask.ndim != 2 or mask.shape != gray.shape or not np.isfinite(mask).all() or (mask < 0).any():
            raise ValueError('图像掩膜形状、数值非法：'+row['uri'])
        border = np.concatenate((gray[0], gray[-1], gray[:, 0], gray[:, -1]))
        foreground = mask > 0
        entry = {'sample_id': sample_id(row['uri']), 'uri': row['uri'], 'mask_uri': row['mask_uri'],
                 'category': row['category'], 'split': row['split'], 'instrument': row['instrument'],
                 'shape': list(mask.shape), 'mask_dtype': str(mask.dtype), 'reference_ids': int(len(np.unique(mask[foreground]))),
                 'foreground_fraction': float(foreground.mean()), 'gray_min': int(gray.min()), 'gray_max': int(gray.max()),
                 'gray_mean': float(gray.mean()), 'gray_std': float(gray.std()),
                 'color_channel_mean_difference': float(np.abs(rgb.astype(float)-rgb[:, :, :1]).mean()),
                 'border_bright_fraction': float((border > 240).mean()),
                 'orientation': 'unknown_no_authoritative_orientation_metadata',
                 'text_detection': 'not_automatically_verified; inspect_actual_input_in_case_view'}
        if entry['gray_std'] < 3 or entry['border_bright_fraction'] > .25:
            issues.append({'sample_id': entry['sample_id'], 'uri': row['uri'], 'kind': 'visual_review_required',
                           'evidence': '低对比度或亮边框启发式，仅作复查提示，未判为坏图'})
        if row['category'] == 'AR':
            restored = np.asarray(Image.fromarray(foreground.astype('uint8')).resize((512, 512), Image.Resampling.NEAREST)
                                  .resize((mask.shape[1], mask.shape[0]), Image.Resampling.NEAREST)) > 0
            from .metrics import binary_segmentation
            entry['mask_512_roundtrip'] = binary_segmentation(restored, foreground)
        grouped[row['category']+' / '+row['split']][row['instrument']] += 1
        items.append(entry)
        if index % 100 == 0:
            status(output, 'D1', '正在核验训练／验证图像与掩膜', processed=index+1, total=len(rows))
    duplicates = defaultdict(list)
    for row in rows:
        duplicates[row['pixel_sha256']].append({'uri': row['uri'], 'split': row['split'], 'category': row['category']})
    value = {'checked_pairs': len(items), 'all_source_fingerprints_verified': True,
             'strata': {k: dict(v) for k, v in grouped.items()}, 'issues': issues,
             'duplicates': [v for v in duplicates.values() if len(v) > 1], 'items': items,
             'label_version': '尚未取得作者确认，不能自动归因于漏标',
             'orientation_limit': '仅核验尺寸、冻结来源与可视对照；没有独立方向真值，不能证明方向正确',
             'test_and_calibration_pixels_opened': False}
    write(output/'audit.json', value)
    return value


def build_assets(output, row, rgb, gray, mask, probability, run_id):
    sid = row['sample_id']
    base = output/'assets'/sid
    def valid(name):
        try:
            with Image.open(base/name) as image:
                image.verify()
            return True
        except (OSError, ValueError, SyntaxError):
            return False
    if not valid('original.png') or not valid('reference.png'):
        png(base/'original.png', rgb)
        rgba = np.zeros((*mask.shape, 4), dtype=np.uint8)
        rgba[mask > 0] = [93, 232, 121, 255]
        png(base/'reference.png', rgba)
    input_name = 'input-'+str(gray.shape[0])+'.png'
    if not valid(input_name):
        png(base/input_name, gray)
    rgba = np.zeros((*mask.shape, 4), dtype=np.uint8)
    rgba[probability >= .5] = [51, 177, 255, 255]
    png(base/(run_id+'-prediction.png'), rgba)
    rgba[:] = 0
    p, y = probability >= .5, mask > 0
    rgba[p & y] = [220, 235, 235, 255]
    rgba[p & ~y] = [255, 86, 86, 255]
    rgba[~p & y] = [255, 195, 61, 255]
    png(base/(run_id+'-errors.png'), rgba)
    return {key: sid+'/'+name for key, name in {'original': 'original.png', 'reference': 'reference.png',
            'input': input_name, 'prediction': run_id+'-prediction.png', 'errors': run_id+'-errors.png'}.items()}


def load_model(experiment, identifier, dataset):
    import torch
    from .train import data_identity, seed_all
    folder = run_folder(experiment, identifier)
    identity = read(folder/'identity.json')
    architecture, expected_identity = model_and_identity(identity['config'])
    if identity['code'] != expected_identity() or identity['data'] != data_identity(dataset):
        raise ValueError('训练源码或数据身份变化，禁止混用历史结果')
    config = read(folder.parent.parent/'configs'/f'{identifier}.json')
    protocol = read(folder.parent.parent/'protocol.json')
    if config != identity['config'] or config != next(c for c in protocol['recognition'] if c['id'] == identifier):
        raise ValueError('冻结配置不一致')
    saved = torch.load(folder/'best.pt', map_location='cpu', weights_only=False)
    if saved['identity'] != identity or not all(torch.isfinite(v).all() for v in saved['model'].values() if torch.is_floating_point(v)):
        raise ValueError('最佳检查点身份或数值非法')
    seed_all(config['seed'])
    model, _ = architecture(config)
    model.load_state_dict(saved['model'])
    model.eval().cuda()
    return model, identity, saved


def benchmark(model, row, source, size, resource):
    import torch
    def load():
        with Image.open(source) as im:
            return torch.from_numpy(np.asarray(im.convert('L').resize((size, size), Image.Resampling.BILINEAR),
                                              dtype=np.float32).copy()[None, None]/255).cuda()
    x = load()
    with torch.inference_mode(), torch.autocast('cuda', dtype=torch.float16):
        for _ in range(10):
            model(x)
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        pure, end_to_end = [], []
        for _ in range(100):
            start = time.perf_counter(); model(x); torch.cuda.synchronize()
            pure.append((time.perf_counter()-start)*1000)
            start = time.perf_counter(); sample = load(); logits = model(sample)
            torch.nn.functional.interpolate(logits.float().sigmoid(), (row['height'], row['width']),
                                            mode='bilinear', align_corners=False).cpu().numpy()
            torch.cuda.synchronize(); end_to_end.append((time.perf_counter()-start)*1000)
    return {'warmup': 10, 'repeats': 100, 'batch': 1, 'parameters': sum(v.numel() for v in model.parameters()),
            'pure_p50_ms': float(np.percentile(pure, 50)), 'pure_p95_ms': float(np.percentile(pure, 95)),
            'end_to_end_p50_ms': float(np.percentile(end_to_end, 50)), 'end_to_end_p95_ms': float(np.percentile(end_to_end, 95)),
            'end_to_end_scope': 'cached_file_read+grayscale+resize+H2D+forward+native_probability+D2H; excludes_rendering',
            'sample_id': row['sample_id'], 'resource': resource.sample()}


def evaluate_run(experiment, output, dataset, identifier, samples, on_progress):
    import torch
    from .datasets import SegmentationDataset
    from .runtime import Resources
    folder = run_folder(experiment, identifier)
    state = read(folder/'status.json')
    model, identity, saved = load_model(experiment, identifier, dataset)
    resource = Resources()
    category, size = identity['config']['category'], identity['config']['size']
    data = SegmentationDataset(dataset, category, 'validation', size)
    chosen = [r for r in samples if r['category'] == category]
    config_key = digest(folder/'best.pt')
    results = []
    try:
        with torch.inference_mode():
            for index, row in enumerate(chosen):
                cache = output/'cases'/identifier/(row['sample_id']+'.json')
                probability_file = output/'probabilities'/identifier/(row['sample_id']+'.npy')
                previous = read(cache)
                if previous:
                    if cache_ready(previous, config_key, probability_file, output/'assets'):
                        results.append(previous)
                        if index % 10 == 0:
                            on_progress(index+1, len(chosen))
                        continue
                with Image.open(data.source(row['uri'])) as im:
                    rgb = np.asarray(im.convert('RGB')).copy()
                    gray = np.asarray(im.convert('L').resize((size, size), Image.Resampling.BILINEAR)).copy()
                x = torch.from_numpy(gray.astype('float32')[None, None]/255).cuda()
                with torch.autocast('cuda', dtype=torch.float16):
                    logits = model(x)
                if not torch.isfinite(logits).all():
                    raise ValueError('诊断推理出现非有限值，未将其当作背景')
                mask = np.load(data.source(row['mask_uri']), allow_pickle=False)
                probability = torch.nn.functional.interpolate(logits.float().sigmoid(), mask.shape,
                    mode='bilinear', align_corners=False)[0, 0].cpu().numpy()
                values = score(probability, mask, category)
                assets = build_assets(output, row, rgb, gray, mask, probability, identifier)
                probability_file.parent.mkdir(parents=True, exist_ok=True)
                tmp = probability_file.with_suffix('.tmp.npy')
                np.save(tmp, probability, allow_pickle=False); tmp.replace(probability_file)
                result = {**values, **{k: row[k] for k in ('sample_id', 'uri', 'mask_uri', 'split', 'instrument', 'category', 'time_utc', 'diagnostic_subset')},
                          'run_id': identifier, 'year': row['time_utc'][:4], 'assets': assets,
                          'checkpoint_sha256': config_key, 'probability_sha256': digest(probability_file),
                          'width': int(mask.shape[1]), 'height': int(mask.shape[0])}
                write(cache, result); results.append(result)
                if index % 10 == 0:
                    resource.sample(); on_progress(index+1, len(chosen))
        validation = [r for r in results if r['split'] == 'validation']
        metrics = summarize(validation)
        expected = saved['validation']['dice']
        delta = abs(metrics['dice']-expected)
        perf = benchmark(model, next(r for r in chosen if r['split'] == 'validation'),
                         data.source(next(r for r in chosen if r['split'] == 'validation')['uri']), size, resource)
        thresholds = [{'threshold': t['threshold'], **{k: float(np.mean([r['thresholds'][i][k] for r in validation]))
                       for k in ('dice', 'iou', 'precision', 'recall')}} for i, t in enumerate(validation[0]['thresholds'])]
        value = {'id': identifier, 'category': category, 'state': 'completed', 'partial_training': state['state'] != 'completed',
                 'checkpoint_epoch': saved['epoch'], 'training_state_at_diagnosis': state['state'],
                 'checkpoint_sha256': config_key, 'validation': metrics, 'strata': stratify(validation),
                 'training_diagnostics': stratify([r for r in results if r['split'] == 'train']),
                 'threshold_sensitivity': thresholds, 'historical_validation_dice': expected,
                 'reproduction_delta': delta, 'reproduction_passed': delta <= 1e-5, 'performance': perf,
                 'case_count': len(results), 'resource': resource.sample()}
        write(output/'models'/(identifier+'.json'), value)
        return value, results
    finally:
        del model
        gc.collect(); torch.cuda.empty_cache()


def conclusions(models, audit_result):
    grouped = defaultdict(list)
    for m in models:
        if m.get('state') == 'completed' and not m['partial_training'] and m['reproduction_passed']:
            grouped[m['id'].rsplit('-s', 1)[0]].append(m)
    rows = []
    for name, group in sorted(grouped.items()):
        values = [m['validation']['dice'] for m in group]
        rows.append({'group': name, 'seeds': len(group), 'mean_dice': float(np.mean(values)),
                     'sd_dice': float(np.std(values, ddof=1)) if len(group) > 1 else None,
                     'pure_p95_ms': float(np.mean([m['performance']['pure_p95_ms'] for m in group])),
                     'peak_allocated_gib': float(np.mean([m['performance']['resource']['peak_allocated_gib'] for m in group]))})
    by_group = {r['group']: r for r in rows}
    recommendations = []
    for row in rows:
        name = row['group']
        if name.startswith('FL-'):
            if name == 'FL-F0':
                label, reason = '保留基线', '保留标准U-Net作同数据对照；尚未证明产品可用。'
            else:
                base = by_group.get('FL-F0')
                if base and row['seeds'] == 3:
                    delta = row['mean_dice']-base['mean_dice']
                    b_models = grouped['FL-F0']
                    stations = sorted(set.intersection(*(set(m['strata']['instrument']) for m in group+b_models)))
                    station_ok = all(np.mean([m['strata']['instrument'][s]['dice'] for m in group])-
                                     np.mean([m['strata']['instrument'][s]['dice'] for m in b_models]) >= -.03 for s in stations)
                    speed = row['pure_p95_ms'] <= base['pure_p95_ms']*.75
                    memory = row['peak_allocated_gib'] <= base['peak_allocated_gib']*.7
                    qualifies = (delta >= .03 and station_ok) or (delta >= -.01 and (speed or memory))
                    label = '针对性验证' if qualifies else '暂缓扩展'
                    reason = f'相对标准U-Net验证Dice差值 {delta*100:+.2f} 个百分点；工程筛选'+('通过，仍需独立验收。' if qualifies else '未通过，不增加训练矩阵。')
                else:
                    label, reason = '证据不足', '缺少完整三种子或基线复算。'
        else:
            label, reason = '建议补齐对照', '当前完成种子不足，先核验错误案例再决定补齐原协议；不增加复杂模型。'
        recommendations.append({'group': name, 'decision': label, 'reason': reason})
    if any(m.get('state') == 'failed' or not m.get('reproduction_passed', False) for m in models):
        recommendations.insert(0, {'group': 'pipeline', 'decision': '先核查流程', 'reason': '存在诊断失败或历史指标不一致，涉及配置的架构结论暂缓。'})
    return rows, recommendations


def publish(output, models, cases, audit_result, finished=False):
    groups, recommendations = conclusions(models, audit_result)
    value = {'schema': SCHEMA, 'report_id': output.name, 'created_utc': now(),
             'state': 'completed' if finished and all(m.get('state') == 'completed' and m.get('reproduction_passed') for m in models) else 'completed_with_issues' if finished else 'running',
             'training_stopped': True, 'test_unlocked': False, 'human_time_study': 'not_performed',
             'models': models, 'groups': groups, 'recommendations': recommendations,
             'paired_comparisons': comparisons(models, cases) if finished else [],
             'audit_summary': {k: v for k, v in audit_result.items() if k != 'items'},
             'limitations': ['仅训练／验证诊断，验证集被重复用于开发，不是独立测试成绩。',
                 '验证集无空参考图，无法据此测独立无目标图像误报率。',
                 '没有专业复核，疑似标注未覆盖不等于已证实漏标。',
                 '未开展人工计时，不能宣称节省标注时间；工程筛选门槛不是产品发布标准。',
                 'AR-A0-s17为失败运行中的有效最佳检查点，只列未完成参考。']}
    write(output/'report.json', value)
    write(output/'case-index.json', cases)
    if finished:
        lines = ['# MDVA 识别效果诊断', '', '训练保持停止；仅训练／验证资料，测试集未打开。', '',
                 '| 配置 | 完成种子 | 验证Dice均值 | 标准差 |', '|---|---:|---:|---:|']
        for r in groups:
            sd = '—' if r['sd_dice'] is None else f"{r['sd_dice']*100:.2f}"
            lines.append(f"| {r['group']} | {r['seeds']} | {r['mean_dice']*100:.2f}% | {sd} 个百分点 |")
        lines += ['', '## 取舍建议', *[f"- {r['group']}：{r['decision']}。{r['reason']}" for r in recommendations],
                  '', '## 限制', *['- '+r for r in value['limitations']], '', '详细逐图数据、台站分层、阈值曲线和资源记录见 report.json 与 case-index.json。']
        (output/'report.md').write_text('\n'.join(lines), encoding='utf-8')
    return value


def run(experiment, output):
    import torch
    from .train import data_identity
    from .runtime import Resources
    from .journal import append
    experiment, output = Path(experiment).resolve(), Path(output).resolve()
    protocol = read(experiment/'protocol.json')
    dataset = Path(protocol['dataset'])
    output_path(output, dataset)
    if output == experiment or output.is_relative_to(experiment) or experiment.is_relative_to(output):
        raise ValueError('诊断目录不能覆盖实验目录')
    ensure_stopped(experiment)
    output.mkdir(parents=True, exist_ok=True)
    numerical = {p.name: digest(p) for p in (Path(__file__).parent/name for name in
                  ('runtime.py', 'models.py', 'stable_models.py', 'metrics.py', 'datasets.py', 'train.py', 'cnn193_fp32.py'))}
    identifiers = []
    for config in protocol['recognition']:
        state = read(run_folder(experiment, config['id'])/'status.json', {})
        if state.get('state') == 'completed' or config['id'] == 'AR-A0-s17' and (run_folder(experiment, config['id'])/'best.pt').exists():
            identifiers.append(config['id'])
    identity = {'schema': SCHEMA, 'experiment': str(experiment), 'data': data_identity(dataset),
                'numerical_code': numerical, 'diagnostic_code': {n: digest(Path(__file__).parent/n) for n in ('diagnose.py', 'diagnostic_support.py', 'diagnostic_review.py', 'diagnostic_statistics.py')},
                'checkpoints': {n: digest(run_folder(experiment, n)/'best.pt') for n in identifiers}}
    previous = read(output/'identity.json')
    if previous and previous != identity:
        raise ValueError('诊断输入或实现已变化，必须建立新报告目录')
    write(output/'identity.json', identity)
    for name in identity['diagnostic_code']:
        snapshot = output/'source'/name
        if not snapshot.exists():
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_bytes((Path(__file__).parent/name).read_bytes())
    with exclusive(experiment/'diagnostic.lock'), exclusive(ROOT/'.mdva/training-gpu.lock'):
        ensure_stopped(experiment)
        try:
            status(output, 'D0', '训练已暂停，检查点与版本身份已登记')
            if not (output/'evidence.json').exists():
                write(output/'evidence.json', {name: read(experiment/name) for name in ('continuation.json', 'queue-state.json', 'watchdog.json')})
            rows = allowed_rows(dataset)
            samples = select_samples(rows)
            write(output/'samples.json', samples)
            audit_result = audit(dataset, output, rows)
            if shutil.disk_usage(output).free < 12*2**30:
                raise ValueError('诊断缓存要求至少12GiB空闲空间')
            torch.cuda.set_per_process_memory_fraction(.75)
            Resources().sample()
            models, cases = [], []
            status(output, 'D2', '开始单GPU诊断推理，不执行训练', models_total=len(identifiers), models_completed=0)
            for number, identifier in enumerate(identifiers):
                ensure_stopped(experiment)
                def update(n, total):
                    status(output, 'D2', '诊断推理：'+identifier, run_id=identifier, processed=n, total=total,
                           models_completed=number, models_total=len(identifiers))
                try:
                    model, results = evaluate_run(experiment, output, dataset, identifier, samples, update)
                    cases.extend(results)
                except Exception as exc:
                    model = {'id': identifier, 'state': 'failed', 'error': str(exc)}
                    write(output/'models'/(identifier+'.json'), model)
                models.append(model)
                publish(output, models, cases, audit_result)
            status(output, 'D4', '生成配对统计与实验取舍建议', models_completed=len(models), models_total=len(identifiers))
            report = publish(output, models, cases, audit_result, finished=True)
            from .diagnostic_review import review
            review(output)
            from .diagnostic_statistics import station_intervals
            station_intervals(output)
            write(output/'status.json', {'state': report['state'], 'phase': 'D4', 'updated_utc': now(),
                  'message': '诊断完成，训练保持停止；等待用户确认后续取舍', 'training_stopped': True,
                  'task_type': 'diagnostic_inference', 'models_completed': sum(m['state'] == 'completed' for m in models),
                  'models_total': len(identifiers)})
            assert numerical == {n: digest(Path(__file__).parent/n) for n in numerical}
            append(experiment, 'handoff', '效果诊断已完成，训练仍停止', '报告位于 '+str(output),
                   details={'report_state': report['state'], 'models': len(models), 'test_accessed': False})
            return report
        except Exception as exc:
            write(output/'status.json', {'state': 'failed', 'phase': 'diagnosis', 'message': str(exc), 'updated_utc': now(),
                  'training_stopped': True, 'task_type': 'diagnostic_inference'})
            raise


if __name__ == '__main__':
    from .runtime import configure_environment
    from .__main__ import DEFAULT_EXPERIMENT
    parser = argparse.ArgumentParser(description='仅训练／验证诊断；不支持校准集或测试集')
    parser.add_argument('--experiment', type=Path, default=DEFAULT_EXPERIMENT)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    configure_environment()
    result = run(args.experiment, args.output)
    print(json.dumps({'state': result['state'], 'report_id': result['report_id']}, ensure_ascii=False))
