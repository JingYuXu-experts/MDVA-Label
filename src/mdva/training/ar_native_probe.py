"""Compare AR input resolutions using the same weights, on CPU development data."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

from .ar_region_retention import read_json
from .datasets import SegmentationDataset
from .durable import atomic_json
from .forecast_region_detector_audit import MODEL, match_reference
from .metrics import binary_segmentation
from .run_locations import model_and_identity
from .runtime import Resources, configure_environment, digest, now, output_path


def run(dataset, package, output):
    import torch
    from safetensors.torch import load_file

    dataset, package = Path(dataset).resolve(), Path(package).resolve()
    output = output_path(output, dataset)
    if output.exists() or output.is_relative_to(package):
        raise ValueError('分辨率对照必须使用新输出目录')
    card_path = package / 'model.json'
    weights_path = package / 'weights.safetensors'
    card = read_json(card_path)
    config = card['config']
    if (card['id'], card['category'], config['size'], card['threshold']) != (MODEL, 'AR', 512, .5):
        raise ValueError('仅接受登记的活动区权重')
    architecture, identity = model_and_identity(config)
    if identity() != card['training_code'] or digest(weights_path) != card['weights_sha256']:
        raise ValueError('权重身份不符')

    data = SegmentationDataset(dataset, 'AR', 'validation')
    groups = {'contains_small_region': [], 'other': []}
    for row in sorted(data.rows, key=lambda item: item['uri']):
        mask = np.load(data.source(row['mask_uri']), allow_pickle=False)
        if mask.shape != (1024, 1024):
            raise ValueError('分辨率对照需要原始1024×1024磁图网格')
        _, areas = np.unique(mask[mask > 0], return_counts=True)
        group = 'contains_small_region' if (areas < 1024).any() else 'other'
        groups[group].append(row)
    generator = np.random.default_rng(2026)
    selected = [(group, rows[index]) for group, rows in groups.items()
                for index in sorted(generator.choice(len(rows), size=12, replace=False))]
    protocol = {
        'created_utc': now(), 'model_card_sha256': digest(card_path),
        'weights_sha256': card['weights_sha256'], 'manifest_sha256': digest(dataset / 'manifest-hashes.json'),
        'code_sha256': digest(__file__), 'input_sides': [512, 1024], 'threshold': .5,
        'precision': 'CPU FP32 for both arms', 'seed': 2026,
        'selection': [{'group': group, 'uri': row['uri']} for group, row in selected],
        'scope': 'Development stress probe, 12 images containing regions under 1024 native pixels and 12 other images. No training.',
        'test_accessed': False,
    }
    output.mkdir(parents=True)
    atomic_json(output / 'protocol.json', protocol)

    torch.set_num_threads(2)
    weights = load_file(str(weights_path))
    if not all(torch.isfinite(tensor).all() for tensor in weights.values()):
        raise ValueError('权重有非有限值')
    model, _ = architecture(config)
    model.load_state_dict(weights)
    model.eval()
    resources = Resources('cpu')
    results = []
    with torch.inference_mode():
        for group, row in selected:
            resources.sample()
            mask = np.load(data.source(row['mask_uri']), allow_pickle=False)
            with Image.open(data.source(row['uri'])) as original:
                image = original.convert('L')
            if image.size != (1024, 1024):
                raise ValueError('输入图像偏离原始网格')
            ids, areas = np.unique(mask[mask > 0], return_counts=True)
            small = set(ids[areas < 1024].tolist())
            result = {'uri': row['uri'], 'group': group, 'arms': {}}
            for side in protocol['input_sides']:
                gray = image.resize((side, side), Image.Resampling.BILINEAR)
                x = torch.from_numpy(np.array(gray, dtype=np.float32) / 255)[None, None]
                logits = model(x)
                if not torch.isfinite(logits).all():
                    raise ValueError('分辨率对照产生非有限值')
                probability = torch.nn.functional.interpolate(
                    logits.sigmoid(), mask.shape, mode='bilinear', align_corners=False
                )[0, 0].numpy()
                prediction = probability >= .5
                matches, total = match_reference(prediction, mask)
                found = {instance for instance, match in matches.items() if match}
                result['arms'][side] = {
                    'reference': len(matches), 'predicted': total, 'matched': len(found),
                    'small_reference': len(small), 'small_matched': len(small & found),
                    'dice': binary_segmentation(prediction, mask > 0)['dice'],
                }
            results.append(result)
            atomic_json(output / 'progress.json', {'images_complete': len(results), 'images_total': len(selected)})

    statistics = {}
    for side in protocol['input_sides']:
        rows = [row['arms'][side] for row in results]
        counts = Counter()
        for row in rows:
            counts.update({key: value for key, value in row.items() if key != 'dice'})
        statistics[side] = {
            **counts, 'macro_dice': float(np.mean([row['dice'] for row in rows])),
            'region_precision': counts['matched'] / counts['predicted'] if counts['predicted'] else None,
            'region_recall': counts['matched'] / counts['reference'],
            'small_region_recall': counts['small_matched'] / counts['small_reference'],
        }
    atomic_json(output / 'images.json', results)
    summary = {
        'state': 'completed', 'images': len(results), 'comparison': statistics,
        'model_training_resolution': 512, 'resources': resources.sample(),
        'protocol_sha256': digest(output / 'protocol.json'), 'images_sha256': digest(output / 'images.json'),
        'interpretation': 'Same weights at different input sizes. Negative result would not rule out training at native scale.',
        'test_accessed': False, 'production_policy_changed': False,
    }
    atomic_json(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset', 'package', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    configure_environment()
    print(run(**vars(args))['comparison'])


if __name__ == '__main__':
    main()
