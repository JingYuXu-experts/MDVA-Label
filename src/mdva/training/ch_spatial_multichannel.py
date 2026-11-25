"""Paired spatial CH pilot on dated, independently registered official observations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image
import torch
import torch.nn.functional as F
from safetensors.torch import save as save_weights

from . import models
from .ch_boundary_growth import remove_small
from .ch_dense_multichannel import aggregate, cards, phenotype, physical, shared_features
from .ch_matched_tiles import CORE, RADIUS, SEED
from .datasets import SegmentationDataset
from .durable import atomic_json, commit
from .journal import read
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


ROOT = Path(__file__).resolve().parents[3]
EPOCHS = 40
BATCH = 2
WIDTH = 8
ARMS = ('aia193', 'aia171_193_211')


def architecture():
    model = models.UNet(width=WIDTH)
    model.encoders[0].layers[0] = torch.nn.Conv2d(4, WIDTH, 3, padding=1)
    return model


def spatial_inputs(arrays):
    feature, support = shared_features(arrays)
    return feature[:, :, [0, 3, 4, 6]].transpose(2, 0, 1).copy(), support


def normalized(data, support, center, scale, channels):
    adjusted = np.where(support[None], (data - center[:, None, None]) / scale[:, None, None], 0.)
    if channels == 'aia193':
        adjusted[2:] = 0.
    return adjusted.astype(np.float32)


def loss(logits, truth, valid):
    # Each observation has equal weight even when the number of supported pixels differs.
    support = valid.sum((1, 2, 3)).clamp_min(1)
    bce_terms = F.binary_cross_entropy_with_logits(logits, truth, reduction='none') * valid
    bce = (bce_terms.sum((1, 2, 3)) / support).mean()
    p = logits.sigmoid() * valid
    y = truth * valid
    axes = (1, 2, 3)
    dice_loss = 1 - ((2 * (p * y).sum(axes) + 1) / ((p + y).sum(axes) + 1)).mean()
    return .5 * (bce + dice_loss)


def read_groups(dataset, dense, sources):
    protocol = read(dense / 'protocol.json')
    report = read(dense / 'summary.json')
    case_path = dense / 'cases.json'
    if (report['protocol_sha256'] != digest(dense / 'protocol.json') or report['cases_sha256'] != digest(case_path)
            or protocol['manifest_sha256'] != digest(dataset / 'manifest-hashes.json')):
        raise ValueError('直接圈选对照身份改变')
    cached = {g: {r['uri']: r for r in c} for g, c in read(case_path).items()}
    data = SegmentationDataset(dataset, 'CH', 'train')
    lookup = {row['uri']: row for row in data.rows}
    training, held = [], []
    for name, folder in sources.items():
        selection = folder / 'selection.json'
        for path in (selection, folder / 'aia/summary.json', folder / 'aia171/summary.json'):
            if protocol['sources'][name].get(path.as_posix()) != digest(path):
                raise ValueError('已核验的多波段来源改变')
        for row, origin, additional in cards(folder, selection):
            uri = row['uri']
            record = lookup[uri]
            is_fit = name == 'earlier' and record['time_utc'] < protocol['fit_before_year']
            original = data.source(uri)
            if row['sha256'] != digest(original):
                raise ValueError('冻结原图身份改变')
            raster = folder / 'aia' / 'pairs' / row['id'] / 'fixed-mapping-193.png'
            with Image.open(original) as jpg, Image.open(raster) as png:
                a, b = [np.asarray(im.convert('L'), dtype=float)[RADIUS <= CORE] for im in (jpg, png)]
            if not (np.corrcoef(a, b)[0, 1] >= .98 and np.mean(abs(a - b)) <= 5):
                continue
            x, supported = spatial_inputs(physical(folder, row, origin, additional))
            truth = np.load(data.source(record['mask_uri']), allow_pickle=False) > 0
            if truth.shape != supported.shape or not supported.any():
                raise ValueError('空间模型输入不完整')
            case = {'id': row['id'], 'uri': uri, 'date': record['date'], 'x': x, 'support': supported, 'truth': truth}
            if is_fit:
                training.append(case)
                continue
            known = cached[name][uri]
            mask = dense / known['mask_artifact']
            if digest(mask) != known['mask_sha256']:
                raise ValueError('上一轮候选资料指纹改变')
            with np.load(mask, allow_pickle=False) as archive:
                if not np.array_equal(supported, archive['support']):
                    raise ValueError('空间与逐像素模型的输入覆盖不同')
            held.append((name, case))
    if len(training) != report['fit_dates'] or len(held) != sum(report['cohort_sizes'].values()):
        raise ValueError('必须复用上次完整的日期对照')
    if set(r['date'] for r in training) & set(r['date'] for _, r in held):
        raise ValueError('拟合和对照日期不可交叠')
    return training, held


def fit(training, name, output, protocol_sha, resource):
    x, y, valid = training
    torch.manual_seed(SEED)
    model = architecture().cuda()
    optimizer = torch.optim.AdamW(model.parameters(), lr=.001, weight_decay=.0001)
    checkpoint = output / f'{name}.last.pt'
    history = []
    if checkpoint.exists():
        saved = torch.load(checkpoint, map_location='cpu', weights_only=True)
        if saved['protocol_sha256'] != protocol_sha or saved['arm'] != name:
            raise ValueError('无法恢复身份不同的检查点')
        model.load_state_dict(saved['model'])
        optimizer.load_state_dict(saved['optimizer'])
        history = saved['history']
    for epoch in range(len(history), EPOCHS):
        if (output / 'PAUSE').exists():
            return None, history
        order = np.random.default_rng(SEED + epoch).permutation(len(y)).tolist()
        totals = []
        for i in range(0, len(order), BATCH):
            batch = order[i:i + BATCH]
            optimizer.zero_grad(set_to_none=True)
            value = loss(model(x[batch]), y[batch], valid[batch])
            if not bool(torch.isfinite(value)):
                raise ValueError('空间模型训练损失出现非有限值')
            value.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5., error_if_nonfinite=True)
            optimizer.step()
            totals.append(float(value.detach()))
        entry = {'epoch': epoch + 1, 'mean_loss': float(np.mean(totals)), 'resources': resource.sample()}
        history.append(entry)
        state = {'protocol_sha256': protocol_sha, 'arm': name, 'model': model.state_dict(),
                 'optimizer': optimizer.state_dict(), 'history': history}
        commit(checkpoint, lambda stream: torch.save(state, stream))
        atomic_json(output / 'status.json', {'state': 'fitting', 'arm': name, **entry})
        if not (epoch + 1) % 10:
            print(json.dumps({'arm': name, **entry}), flush=True)
    return model.eval(), history


def run(dataset, earlier, fresh, dense, output):
    output = output_path(output, dataset)
    training, held = read_groups(dataset, dense, {'earlier': earlier, 'fresh': fresh})
    stacked = np.concatenate([r['x'][:, r['support']] for r in training], axis=1)
    center = stacked.mean(axis=1)
    scale = np.maximum(stacked.std(axis=1), .01)
    protocol = {'code_sha256': digest(__file__), 'architecture_sha256': digest(models.__file__),
                'reference_summary_sha256': digest(dense / 'summary.json'), 'seed': SEED, 'epochs': EPOCHS,
                'batch_size': BATCH, 'input': ('log193', 'relative_radius', 'log171_minus_log193', 'log211_minus_log193'),
                'control_ratio_channels_zeroed': True, 'train_mean': center.tolist(), 'train_std': scale.tolist(),
                'fit': [r['uri'] for r in training], 'evaluation': [r['uri'] for _, r in held],
                'core_threshold': .50, 'precision': 'FP32', 'test_accessed': False,
                'validation_accessed': False, 'calibration_accessed': False}
    protocol_path = output / 'protocol.json'
    if (output / 'summary.json').exists():
        raise ValueError('已完成报告不能覆盖')
    if protocol_path.exists() and read(protocol_path) != json.loads(json.dumps(protocol)):
        raise ValueError('续跑协议身份改变')
    if not protocol_path.exists():
        atomic_json(protocol_path, protocol)
    protocol_sha = digest(protocol_path)
    results = {}
    details = {}
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.set_num_threads(2)
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cuda.matmul.allow_tf32 = False
        resource = Resources()
        y, supported = [torch.tensor(np.stack([r[key] for r in training]), dtype=torch.float32, device='cuda')[:, None]
                        for key in ('truth', 'support')]
        for name in ARMS:
            x = torch.from_numpy(np.stack([normalized(r['x'], r['support'], center, scale, name) for r in training])).cuda()
            model, history = fit((x, y, supported), name, output, protocol_sha, resource)
            if model is None:
                state = {'state': 'paused', 'arm': name, 'epoch': len(history)}
                atomic_json(output / 'status.json', state)
                return state
            pack = output / f'{name}.research.safetensors'
            commit(pack, lambda stream: stream.write(save_weights({k: v.cpu().contiguous() for k, v in model.state_dict().items()})))
            details[name] = {'parameters': sum(t.numel() for t in model.parameters()), 'weights_sha256': digest(pack),
                             'mean_loss_first': history[0]['mean_loss'], 'mean_loss_last': history[-1]['mean_loss']}
            with torch.inference_mode():
                for group, row in held:
                    inputs = torch.from_numpy(normalized(row['x'], row['support'], center, scale, name))[None].cuda()
                    logit = model(inputs)[0, 0].cpu().numpy()
                    if not np.isfinite(logit).all():
                        raise ValueError('空间模型输出含非有限值')
                    predicted = remove_small((logit >= 0) & row['support'])
                    file = output / 'masks' / name / f'{row["id"]}.npz'
                    commit(file, lambda stream: np.savez_compressed(stream, prediction=predicted, support=row['support']))
                    card = results.setdefault(group, {}).setdefault(row['uri'], {'uri': row['uri'], 'date': row['date'], 'arms': {}})
                    card['arms'][name] = {**phenotype(predicted, row['truth'], row['support']),
                                          'mask_artifact': file.relative_to(output).as_posix(), 'mask_sha256': digest(file)}
            del model, x
        resources = resource.sample()
    groups = {g: list(items.values()) for g, items in results.items()}
    atomic_json(output / 'cases.json', groups)
    summary = {'state': 'completed', 'recorded_utc': now(), 'protocol_sha256': protocol_sha,
                'cases_sha256': digest(output / 'cases.json'), 'fit_images': len(training),
                'scored_images': {g: len(items) for g, items in groups.items()},
                'metrics': {g: aggregate(items) for g, items in groups.items()}, 'models': details, 'resources': resources,
                'deployed': False, 'test_accessed': False, 'validation_accessed': False, 'calibration_accessed': False}
    atomic_json(output / 'summary.json', summary)
    atomic_json(output / 'status.json', {'state': 'completed', 'epochs_per_arm': EPOCHS,
                                        'summary_sha256': digest(output / 'summary.json')})
    return summary


if __name__ == '__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('dataset', 'earlier', 'fresh', 'dense', 'output'):
        parser.add_argument('--' + argument, type=Path, required=True)
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))
