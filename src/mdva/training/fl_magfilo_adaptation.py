"""Test whether time-separated GONG supervision improves FL transfer."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import label
import torch

from . import reviewed_train as engine
from .datasets import SegmentationDataset
from .diagnostic_support import connected_scores
from .durable import atomic_json
from .fl_author_model import AuthorGraph, loss_value
from .fl_ensemble_study import CONNECTIVITY, DATASET, PILOT, ROOT
from .fl_magfilo_audit import compare, raster
from .fl_magfilo_transfer import inventory, load_gray, metrics
from .fl_pilot_review import region_summary
from .journal import append, read
from .metrics import binary_segmentation, segmentation_summary
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path

ARM_NAMES = ('local_only', 'mixed_gong')
SWAP = 128
EPOCHS = 2
BASE_IDENTITY = engine.code_identity


def identity():
    names = ('fl_magfilo_adaptation.py', 'fl_magfilo_audit.py', 'fl_magfilo_transfer.py', 'fl_author_model.py')
    return {**BASE_IDENTITY(), **{name: digest(Path(__file__).with_name(name)) for name in names}}


class FrozenNormUNet(AuthorGraph):
    def train(self, mode=True):
        super().train(mode)
        if mode:
            for module in self.modules():
                if isinstance(module, torch.nn.BatchNorm2d):
                    module.eval()
        return self


def stratified(rows, count, time_key):
    ordered = sorted(rows, key=lambda row: row[time_key])
    if len(ordered) < count:
        raise ValueError('配对训练的早期样本数量不足')
    indices = np.linspace(0, len(ordered) - 1, count).round().astype(int)
    return [ordered[i] for i in indices]


def source_receipt(folder, expected_role, registered):
    receipt, plan = (read(folder / name) for name in ('summary.json', 'protocol.json'))
    gallery = folder / 'inventory.json'
    if (receipt['state'] != 'completed' or receipt['test_accessed'] or plan['role'] != expected_role
            or receipt['inventory_sha256'] != digest(gallery)):
        raise ValueError('FL 跨来源数据收据不符')
    images = read(gallery)
    if len({row['file'] for row in images}) != len(images):
        raise ValueError('FL 跨来源数据存在重复文件')
    for row in images:
        base = registered[row['file']]
        if row['role'] != expected_role or any(row[key] != value for key, value in base.items()):
            raise ValueError('FL 跨来源收据中的日期或标签身份改变')
        if digest(folder / 'jpeg' / row['file']) != row['image_sha256']:
            raise ValueError('FL 跨来源收据中的图像变化')
    return images


def dataset_for(selected, folder, annotations):
    class PairedDataset(SegmentationDataset):
        def __init__(self, path, category, split, size):
            super().__init__(path, category, split, size)
            if split == 'train':
                self.rows = selected

        def __getitem__(self, i):
            row = self.rows[i]
            if 'file' not in row:
                return super().__getitem__(i)
            raw = (folder / 'jpeg' / row['file']).read_bytes()
            if sha256(raw).hexdigest() != row['image_sha256']:
                raise ValueError('外部训练图像身份变化')
            masks = [raster(annotations[a['id']]) for a in row['annotators']]
            votes = np.mean(masks, axis=0, dtype=np.float32)
            return {'image': torch.from_numpy(load_gray(raw))[None], 'semantic_mask': torch.from_numpy(votes)[None]}

    return PairedDataset


def train(output, arm, data_class, source, plan):
    run = output / arm
    config = {
        'id': f'FL-MAGFiLO-{arm}-s2026', 'category': 'FL', 'model': 'fl_author_unet',
        'width': 32, 'size': 512, 'loss': 'bce_dice', 'seed': 2026,
        'augment': False, 'station_balance': False, 'micro_batch': 1, 'effective_batch': 8,
        'max_epochs': EPOCHS, 'min_epochs': EPOCHS, 'lr': 2e-5,
        'protocol_sha256': digest(output / 'protocol.json'),
    }
    previous = read(run / 'status.json', {})
    if previous.get('state') == 'completed':
        return config
    if (output / 'PAUSE').exists() or (run / 'PAUSE').exists():
        return None

    class Optim:
        lr_scheduler = torch.optim.lr_scheduler

        def Adam(self, parameters, **_):
            return torch.optim.Adam(parameters, lr=config['lr'])

    class Torch:
        optim = Optim()

        def __getattr__(self, key):
            return getattr(torch, key)

    def architecture(_):
        model = FrozenNormUNet('batch')
        model.load_state_dict(source)
        return model, {'weights_sha256': plan['warm_start_sha256'], 'running_moments': 'frozen'}

    originals = (engine.architecture, engine.code_identity, engine.SegmentationDataset, engine.loss_value, engine.torch)
    append(ROOT / 'out/training-20261112-v2', 'development', '暗条 GONG 配对适配', f'{arm} · {EPOCHS} 轮', run_id=config['id'])
    try:
        engine.architecture = architecture
        engine.code_identity = identity
        engine.SegmentationDataset = data_class
        engine.loss_value = lambda logits, target, _: loss_value(logits, target, 10)
        engine.torch = Torch()
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cuda.matmul.allow_tf32 = False
        state = engine.train(config, DATASET, run, resume=(run / 'last.pt').exists())
    finally:
        engine.architecture, engine.code_identity, engine.SegmentationDataset, engine.loss_value, engine.torch = originals
    return config if state['state'] == 'completed' else None


def evaluate(out, configs, local, external, folder, annotation_table):
    results = {}
    with exclusive(ROOT / '.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cuda.matmul.allow_tf32 = False
        usage = Resources()
        for arm, config in configs.items():
            weights = out / arm / 'last.pt'
            saved = engine.load_checkpoint(weights)
            if saved['epoch'] != EPOCHS or saved['identity']['config'] != config or saved['identity']['code'] != identity():
                raise ValueError('配对权重与登记状态不符')
            model = FrozenNormUNet('batch')
            model.load_state_dict(saved['model'])
            model.cuda().eval()
            cases = []
            for i, row in enumerate(local.rows):
                native = np.load(local.source(row['mask_uri']), allow_pickle=False) > 0
                ref = label(native, structure=CONNECTIVITY)[0]
                mask = prediction(model, local[i]['image'][None])
                cases.append({'uri': row['uri'], 'pixel': binary_segmentation(mask, native), 'regions': connected_scores(mask, ref)})
            pixels = segmentation_summary([row['pixel'] for row in cases])
            if abs(pixels['dice'] - saved['history'][-1]['validation']['dice']) > 1e-5:
                raise ValueError('配对训练完成后与训练期开发分数不一致')
            cross = []
            for row in external:
                raw = (folder / 'jpeg' / row['file']).read_bytes()
                if sha256(raw).hexdigest() != row['image_sha256']:
                    raise ValueError('外部开发图像身份变化')
                mask = prediction(model, torch.from_numpy(load_gray(raw))[None, None])
                cross.append({'file': row['file'], 'comparisons': [
                    compare(mask, raster(annotation_table[a['id']])) for a in row['annotators']
                ]})
            atomic_json(out / arm / 'development.json', {'local': cases, 'gong': cross})
            results[arm] = {'local': {'pixels': pixels, 'regions': region_summary([r['regions'] for r in cases])},
                            'gong': metrics(cross), 'weights_sha256': digest(weights)}
            usage.sample()
        return results, usage.sample()


def prediction(model, image):
    logits = model(image.cuda())
    if not torch.isfinite(logits).all():
        raise ValueError('暗条复核出现非有限结果')
    return logits.sigmoid()[0, 0].cpu().numpy() >= .5


def run(source, release, early, later, output):
    output = output_path(output, DATASET)
    if (output / 'summary.json').exists():
        raise ValueError('配对试验已封存')
    if any(output.is_relative_to(folder.resolve()) for folder in (early, later)):
        raise ValueError('训练输出与外部输入必须独立')
    registered, annotations = inventory(source, release)
    indexed = {r['file']: r for r in registered}
    training = source_receipt(early, 'train_available', indexed)
    transfer = source_receipt(later, 'transfer_dev', indexed)
    refs = SegmentationDataset(DATASET, 'FL', 'train', 512)
    dev = SegmentationDataset(DATASET, 'FL', 'validation', 512)
    local = stratified([r for r in refs.rows if '2011' <= r['time_utc'] < '2016'], SWAP * 2, 'time_utc')
    external = stratified(training, SWAP, 'captured_utc')
    samples = {'local_only': local[::2] + local[1::2], 'mixed_gong': local[::2] + external}
    checkpoint = PILOT / 'checkpoint.pt'
    start = read(PILOT / 'summary.json')
    if start['checkpoint_sha256'] != digest(checkpoint) or start['test_accessed']:
        raise ValueError('配对起点身份变化')
    warm = engine.load_checkpoint(checkpoint)['model']
    plan = {
        'warm_start_sha256': digest(checkpoint), 'identity': identity(), 'epochs': EPOCHS,
        'local_training_uris': [r['uri'] for r in local], 'gong_training_images': [r['file'] for r in external],
        'early_inventory_sha256': digest(early / 'inventory.json'), 'later_inventory_sha256': digest(later / 'inventory.json'),
        'threshold': .5, 'paired_change': 'replace 128 local observations with 128 GONG observations, both from 2011–2015',
        'screening_gate': 'GONG strict recall +3pp and precision loss at most 1pp versus both controls; local recall loss at most 1pp versus warm start',
        'scope': 'local research, CC BY-NC 4.0 external data, no final evaluation',
        'test_accessed': False, 'calibration_accessed': False,
    }
    marker = output / 'protocol.json'
    if marker.exists() and read(marker) != plan:
        raise ValueError('配对方案变化，不能复用旧运行')
    atomic_json(marker, plan)
    configs = {}
    for arm in ARM_NAMES:
        config = train(output, arm, dataset_for(samples[arm], early, annotations), warm, plan)
        if config is None:
            return {'state': 'paused'}
        configs[arm] = config
    results, resource = evaluate(output, configs, dev, transfer, later, annotations)
    local_orig = start['regions']
    gong_orig = read(later / 'summary.json')['transfer_dev']
    gong_ctl, gong_mix = (results[a]['gong'] for a in ARM_NAMES)
    passed = (
        gong_mix['recall_iou_050'] >= max(gong_ctl['recall_iou_050'], gong_orig['recall_iou_050']) + .03
        and gong_mix['precision_iou_050'] >= max(gong_ctl['precision_iou_050'], gong_orig['precision_iou_050']) - .01
        and results['mixed_gong']['local']['regions']['recall'] >= local_orig['recall'] - .01
    )
    result = {
        'state': 'completed', 'created_utc': now(), 'results': results,
        'warm_start': {'local': local_orig, 'gong': gong_orig}, 'screening_gate_passed': passed,
        'protocol_sha256': digest(marker), 'resources': resource,
        'test_accessed': False, 'calibration_accessed': False, 'deployed': False,
    }
    atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'release', 'early', 'later', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    configure_environment()
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))


if __name__ == '__main__':
    main()
