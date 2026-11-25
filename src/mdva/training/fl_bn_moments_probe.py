"""Isolate the effect of FL BatchNorm running statistics with frozen weights."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import label

from .ar_region_retention import read_json
from .datasets import SegmentationDataset
from .diagnostic_support import connected_scores, sample_id
from .durable import atomic_json
from .fl_author_model import AuthorGraph, SOURCE
from .fl_pilot_review import region_summary
from .metrics import binary_segmentation, segmentation_summary
from .reviewed_train import data_identity, load_checkpoint
from .runtime import Resources, configure_environment, digest, now, output_path


def run(study, archived_review, output):
    import torch

    study, archived_review = Path(study).resolve(), Path(archived_review).resolve()
    plan = read_json(study / 'protocol.json')
    archive = read_json(archived_review / 'summary.json')
    checkpoint = archived_review / 'checkpoint.pt'
    dataset = plan['dataset']
    output = output_path(output, dataset)
    if output.exists() or any(output.is_relative_to(path) for path in (study, archived_review)):
        raise ValueError('统计对照需要独立输出目录')
    if (
        archive['state'] != 'completed' or archive['test_accessed'] or archive['calibration_accessed']
        or archive['plan_sha256'] != digest(study / 'protocol.json')
        or archive['checkpoint_sha256'] != digest(checkpoint)
        or plan['data_identity'] != data_identity(dataset)
        or plan['source_provenance_sha256'] != digest(SOURCE / 'provenance.json')
    ):
        raise ValueError('试点检查点、代码或冻结清单身份不符')

    train = SegmentationDataset(dataset, 'FL', 'train', 512)
    validation = SegmentationDataset(dataset, 'FL', 'validation', 512)
    if len(train) != 3033 or len(validation) != 182:
        raise ValueError('开发样本与协议不符')
    stations = sorted({row['instrument'] for row in train.rows})
    if len(stations) != 3:
        raise ValueError('需要三座台站供混合统计')
    rng = np.random.default_rng(2026)
    selected = [rng.choice(sorted(row['uri'] for row in train.rows if row['instrument'] == station),
                           size=32, replace=False).tolist() for station in stations]
    batches = list(zip(*selected))
    protocol = {
        'created_utc': now(), 'checkpoint_sha256': archive['checkpoint_sha256'],
        'archived_review_sha256': digest(archived_review / 'summary.json'),
        'epoch': archive['epoch'], 'code_sha256': digest(__file__), 'threshold': .5, 'seed': 2026,
        'train_batches': batches, 'stations': stations,
        'scope': 'Uniform mixed-station batches from training images, without masks. Evaluate all182 development images.',
        'hypothesis': 'Changing only running moments may improve the final checkpoint; compare complete control results including station-specific failures.',
    }
    output.mkdir(parents=True)
    atomic_json(output / 'protocol.json', protocol)

    torch.set_num_threads(2)
    saved = load_checkpoint(checkpoint)
    if saved['epoch'] != archive['epoch'] or saved['identity'] != read_json(study / 'run/identity.json'):
        raise ValueError('检查点与独立复核不一致')
    model = AuthorGraph('batch')
    model.load_state_dict(saved['model'])
    model.eval()
    model.requires_grad_(False)
    bn = [layer for layer in model.modules() if isinstance(layer, torch.nn.BatchNorm2d)]
    if not bn:
        raise ValueError('模型没有可核验的 BatchNorm 层')
    bn_before = [layer.running_mean.clone() for layer in bn]
    for layer in bn:
        layer.reset_running_stats()
        layer.momentum = None
        layer.train()

    resource = Resources('cpu')
    with torch.inference_mode():
        for batch in batches:
            values = []
            for uri in batch:
                with Image.open(train.source(uri)) as image:
                    gray = image.convert('L').resize((512, 512), Image.Resampling.BILINEAR)
                    values.append(np.array(gray, dtype=np.float32) / 255)
            logits = model(torch.from_numpy(np.stack(values))[:, None])
            if not torch.isfinite(logits).all():
                raise ValueError('BatchNorm 分布估计出现非有限值')
            resource.sample()
        model.eval()
        if any(not torch.equal(parameter, saved['model'][name]) for name, parameter in model.named_parameters()):
            raise ValueError('对照过程中可训练参数发生变化')
        records = []
        for index, row in enumerate(validation.rows):
            item = validation[index]
            logits = model(item['image'][None])
            if not torch.isfinite(logits).all():
                raise ValueError('验证出现非有限值')
            reference = np.load(validation.source(row['mask_uri']), allow_pickle=False) > 0
            prediction = logits.sigmoid()[0, 0].numpy() >= .5
            previous = read_json(archived_review / 'cases' / (sample_id(row['uri']) + '.json'))
            if previous['uri'] != row['uri'] or previous['instrument'] != row['instrument']:
                raise ValueError('历史逐图记录与当前数据不一致')
            score = binary_segmentation(prediction, reference, boundary=True)
            objects = connected_scores(prediction, label(reference, structure=np.ones((3, 3)))[0])
            records.append({'uri': row['uri'], 'station': row['instrument'],
                            'before': {'score': previous['score'], 'objects': previous['regions']},
                            'after': {'score': score, 'objects': objects}})
            resource.sample()
            if (index + 1) % 25 == 0:
                atomic_json(output / 'progress.json', {'validation_images_complete': index + 1, 'total': len(validation)})

    def stats(rows, key):
        return {
            'pixels': segmentation_summary([row[key]['score'] for row in rows]),
            'regions': region_summary([row[key]['objects'] for row in rows]),
        }

    result = {key: stats(records, key) for key in ('before', 'after')}
    if result['before']['regions'] != archive['regions'] or result['before']['pixels'] != archive['metrics']:
        raise ValueError('对照臂无法逐图复算')
    atomic_json(output / 'images.json', records)
    summary = {
        'state': 'completed', 'created_utc': now(), 'epoch': archive['epoch'], 'comparison': result,
        'by_station': {station: {key: stats([row for row in records if row['station'] == station], key)
                                 for key in ('before', 'after')} for station in stations},
        'bn_layers': len(bn), 'bn_layers_changed': sum(not torch.equal(layer.running_mean, old) for layer, old in zip(bn, bn_before)),
        'unchanged_trainable_parameters': True, 'resources': resource.sample(),
        'protocol_sha256': digest(output / 'protocol.json'), 'rows_sha256': digest(output / 'images.json'),
        'test_accessed': False, 'weights_replaced': False, 'training': False,
    }
    atomic_json(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('study', 'archived-review', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    configure_environment()
    print(run(**vars(args))['comparison'])


if __name__ == '__main__':
    main()
