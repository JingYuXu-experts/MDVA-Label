"""Test added CH training dates against an equal-update repeated-date control."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image
from safetensors.torch import load_file, save as save_weights
import torch

from .ch_dense_multichannel import aggregate, cards, phenotype, physical
from .ch_matched_tiles import CORE, RADIUS
from .ch_boundary_growth import remove_small
from .ch_spatial_multichannel import ARMS, EPOCHS, BATCH, ROOT, architecture, fit, normalized, read_groups, spatial_inputs
from .datasets import SegmentationDataset
from .durable import atomic_json, commit
from .journal import read
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


def expansion_cases(dataset, folder, manifest, prohibited_dates):
    path = folder / 'selection.json'
    selection = read(path)
    if selection['manifest_sha256'] != manifest:
        raise ValueError('扩样清单身份不同')
    possible = {item['uri']: item for item in selection['cohort']}
    if len(possible) != 16 or any(i['role'] not in ('additional_fit', 'new_check') for i in possible.values()):
        raise ValueError('需要原先锁定的训练与对照日期')
    if any(r['date'] in prohibited_dates for r in possible.values()) or len({r['date'] for r in possible.values()}) != 16:
        raise ValueError('扩样日期与既往开发日期相交')
    data = SegmentationDataset(dataset, 'CH', 'train')
    lookup = {r['uri']: r for r in data.rows}
    cases = {'additional_fit': [], 'new_check': []}
    missing = set(possible)
    for row, a, b in cards(folder, path):
        uri = row['uri']
        specification = possible[uri]
        focal = lookup[uri]
        if specification['date'] != focal['date'] or digest(data.source(uri)) != row['sha256']:
            raise ValueError('原始数据指纹或名义日期不符')
        preview = folder / 'aia' / 'pairs' / row['id'] / 'fixed-mapping-193.png'
        with Image.open(data.source(uri)) as photo, Image.open(preview) as mapped:
            left, right = [np.asarray(im.convert('L'), dtype=float)[RADIUS <= CORE] for im in (photo, mapped)]
        if not (np.corrcoef(left, right)[0, 1] >= .98 and np.mean(abs(left - right)) <= 5):
            continue
        x, support = spatial_inputs(physical(folder, row, a, b))
        truth = np.load(data.source(focal['mask_uri']), allow_pickle=False) > 0
        if truth.shape != support.shape or not support.any():
            raise ValueError('参考与已登记的共同网格不符')
        cases[specification['role']].append({'id': row['id'], 'uri': uri, 'date': specification['date'],
                                           'x': x, 'support': support, 'truth': truth})
        missing.remove(uri)
    return cases, [possible[uri] for uri in sorted(missing)]


def statistics(rows):
    arrays = np.concatenate([r['x'][:, r['support']] for r in rows], axis=1)
    return arrays.mean(axis=1), np.maximum(arrays.std(axis=1), .01)


def evaluate(model, name, check, location, center, scale):
    results = []
    with torch.inference_mode():
        for row in check:
            x = torch.from_numpy(normalized(row['x'], row['support'], center, scale, name))[None].cuda()
            logits = model(x)[0, 0].cpu().numpy()
            if not np.isfinite(logits).all():
                raise ValueError('冕洞空间模型输出含非有限值')
            prediction = remove_small((logits >= 0.) & row['support'])
            file = location / 'masks' / name / (row['id'] + '.npz')
            commit(file, lambda stream: np.savez_compressed(stream, prediction=prediction, support=row['support']))
            results.append({**phenotype(prediction, row['truth'], row['support']), 'mask_path': file.as_posix(),
                            'mask_sha256': digest(file)})
    return results


def run(dataset, earlier, fresh, dense, baseline, expansion, output):
    output = output_path(output, dataset)
    archived = read(baseline / 'summary.json')
    baseline_protocol = read(baseline / 'protocol.json')
    if (archived['state'] != 'completed' or archived['protocol_sha256'] != digest(baseline / 'protocol.json')
            or baseline_protocol['reference_summary_sha256'] != digest(dense / 'summary.json')):
        raise ValueError('原空间实验指纹不一致')
    original, studied = read_groups(dataset, dense, {'earlier': earlier, 'fresh': fresh})
    dates = {r['date'] for r in original} | {row['date'] for _, row in studied}
    groups, missing = expansion_cases(dataset, expansion, digest(dataset / 'manifest-hashes.json'), dates)
    extra, check = (groups[key] for key in ('additional_fit', 'new_check'))
    if len(extra) != len(original) or len(check) < 6:
        raise ValueError('不足以按相同的更新次数对照或评估')
    if (output / 'summary.json').exists():
        raise ValueError('完成的扩样报告不可覆盖')
    experiments = {'archived8': original, 'repeat8': original * 2, 'expanded16': original + extra}
    identity = {'code_sha256': digest(__file__), 'baseline_sha256': digest(baseline / 'summary.json'),
                'selected_sha256': digest(expansion / 'selection.json'),
                'triplet_sha256': digest(expansion / 'aia171/summary.json'), 'epochs': EPOCHS, 'batch_size': BATCH,
                'updates': {k:EPOCHS * len(rows) // BATCH for k, rows in experiments.items()},
                'cases': {k:[r['uri'] for r in v] for k, v in experiments.items()}, 'new_check': [r['uri'] for r in check],
                'missing': missing, 'test_accessed': False, 'validation_accessed': False, 'calibration_accessed': False}
    protocol_file = output / 'protocol.json'
    if protocol_file.exists() and read(protocol_file) != identity:
        raise ValueError('续跑实验身份改变')
    if not protocol_file.exists():
        atomic_json(protocol_file, identity)
    details = {}
    cases = {}
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.set_num_threads(2)
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cuda.matmul.allow_tf32 = False
        resources = Resources()
        for name, training in experiments.items():
            folder = output / name
            center, scale = (np.array(baseline_protocol['train_mean']), np.array(baseline_protocol['train_std'])) if name == 'archived8' else statistics(training)
            targets, support = [torch.tensor(np.stack([r[k] for r in training]), dtype=torch.float32, device='cuda')[:, None]
                                for k in ('truth', 'support')]
            evaluated = [{'uri': r['uri'], 'date': r['date'], 'arms': {}} for r in check]
            details[name] = {}
            for arm in ARMS:
                weight = baseline / f'{arm}.research.safetensors'
                if name == 'archived8':
                    if digest(weight) != archived['models'][arm]['weights_sha256']:
                        raise ValueError('既有空间模型权重身份不符')
                    model = architecture().cuda()
                    model.load_state_dict(load_file(weight))
                    model.eval()
                else:
                    x = torch.from_numpy(np.stack([normalized(r['x'], r['support'], center, scale, arm) for r in training])).cuda()
                    model, history = fit((x, targets, support), arm, folder, digest(protocol_file), resources)
                    if model is None:
                        return {'state': 'paused', 'arm': arm, 'experiment': name, 'epoch': len(history)}
                    weight = folder / f'{arm}.research.safetensors'
                    commit(weight, lambda stream: stream.write(save_weights({k:v.cpu().contiguous() for k, v in model.state_dict().items()})))
                    del x
                details[name][arm] = {'weights_sha256': digest(weight)}
                for row, score in zip(evaluated, evaluate(model, arm, check, folder, center, scale), strict=True):
                    row['arms'][arm] = score
                del model
            cases[name] = evaluated
            del targets, support
        resource_record = resources.sample()
    atomic_json(output / 'cases.json', cases)
    summary = {'state': 'completed', 'recorded_utc': now(), 'protocol_sha256': digest(protocol_file),
               'cases_sha256': digest(output / 'cases.json'), 'metrics': {k:aggregate(v) for k,v in cases.items()},
               'models': details, 'resources': resource_record, 'scored_new_dates': len(check), 'missing': missing,
               'test_accessed': False, 'validation_accessed': False, 'calibration_accessed': False, 'deployed': False}
    atomic_json(output / 'summary.json', summary)
    return summary


if __name__ == '__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('dataset', 'earlier', 'fresh', 'dense', 'baseline', 'expansion', 'output'):
        parser.add_argument('--' + argument, type=Path, required=True)
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))
