"""Train an independent presence classifier and replay the same added AR masks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import numpy as np
from scipy.ndimage import binary_dilation
import torch
from torch import nn
from torch.nn import functional as F

from .ar_bipolar_proposals import image_data
from .ar_patch_refiner import central_mask, crop_array, open_study
from .ar_scale_review import estimate, native_objects
from .ar_scale_study import ROOT
from .datasets import SegmentationDataset
from .durable import atomic_checkpoint, atomic_json
from .forecast_region_detector_audit import MODEL
from .journal import read
from .metrics import binary_segmentation
from .models import UNet
from .reviewed_train import load_checkpoint, restore_rng, rng_state, seed_all
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


BUDGET = 10
BATCH = 32
THRESHOLDS = {'verify50': .5, 'verify80': .8}
SEED = 2026


class PresenceClassifier(nn.Module):
    def __init__(self):
        super().__init__()
        self.encoder = nn.Sequential(
            nn.Conv2d(1, 16, 5, stride=2, padding=2), nn.ReLU(),
            nn.Conv2d(16, 24, 3, stride=2, padding=1), nn.ReLU(),
            nn.Conv2d(24, 32, 3, stride=2, padding=1), nn.ReLU(),
        )
        self.classifier = nn.Linear(64, 1)

    def forward(self, images):
        encoded = self.encoder(images).flatten(2)
        pooled = torch.cat((encoded.mean(2), encoded.amax(2)), dim=1)
        return self.classifier(pooled).squeeze(1)


def train_presence(study, output, resource, protocol_hash):
    with np.load(study / 'patches.npz', allow_pickle=False) as archive:
        images = torch.from_numpy(archive['images'])[:, None]
        mask = archive['targets']
    target = torch.from_numpy((mask > 0).any((1, 2))).float()
    if (len(images), int(target.sum()), tuple(images.shape[1:])) != (4950, 1134, (1, 64, 64)):
        raise ValueError('局部真伪标签或训练裁片不同')
    seed_all(SEED)
    model = PresenceClassifier().cuda()
    opt = torch.optim.Adam(model.parameters(), lr=.001)
    checkpoint = output / 'last.pt'
    start, history = 0, []
    if checkpoint.exists():
        saved = load_checkpoint(checkpoint)
        if saved['protocol_sha256'] != protocol_hash:
            raise ValueError('复核分类检查点身份不符')
        model.load_state_dict(saved['model'])
        opt.load_state_dict(saved['optimizer'])
        restore_rng(saved['rng'])
        start, history = saved['epoch'], saved['history']
    for epoch in range(start + 1, BUDGET + 1):
        if (output / 'PAUSE').exists():
            atomic_json(output / 'status.json', {'state': 'paused', 'epoch': epoch - 1})
            return None
        stamp = time.perf_counter()
        order = torch.randperm(len(images), generator=torch.Generator().manual_seed(SEED + epoch))
        total = 0.
        for selection in order.split(BATCH):
            x, y = images[selection].cuda().float() / 255, target[selection].cuda()
            opt.zero_grad(set_to_none=True)
            loss = F.binary_cross_entropy_with_logits(model(x), y)
            if not torch.isfinite(loss):
                raise ValueError('复核分类损失非有限')
            loss.backward()
            opt.step()
            total += float(loss.detach()) * len(selection)
        history.append({'epoch': epoch, 'loss': total / len(images), 'seconds': time.perf_counter() - stamp})
        atomic_checkpoint(checkpoint, {'protocol_sha256': protocol_hash, 'epoch': epoch, 'history': history,
                                       'model': model.state_dict(), 'optimizer': opt.state_dict(), 'rng': rng_state()})
        atomic_json(output / 'status.json', {'state': 'training', 'epoch': epoch, 'budget': BUDGET,
                                            'resources': resource.sample()})
    return model.eval()


def separate_mask(augmented, patch, box):
    x0, y0, x1, y1 = box
    avoid = binary_dilation(augmented[max(0, y0-1):y1+1, max(0, x0-1):x1+1])
    top, left = int(y0 > 0), int(x0 > 0)
    return patch.sum() >= 4 and not np.any(patch & avoid[top:top+y1-y0, left:left+x1-x0])


def reproduce(model, classifier, data, regions, frozen, diagnostic, output, resource):
    rows = []
    with torch.inference_mode():
        for index, item in enumerate(data.rows, 1):
            uri = item['uri']
            gray, reference = image_data(data, item)
            case = frozen[uri]
            path = diagnostic / 'probabilities' / MODEL / (case['sample_id'] + '.npy')
            if digest(path) != case['probability_sha256'] or case['mask_uri'] != item['mask_uri']:
                raise ValueError('旧检测器输出指纹不符')
            probability = np.load(path, allow_pickle=False)
            if probability.shape != reference.shape or not np.isfinite(probability).all():
                raise ValueError('原始概率误差')
            masks = {arm: probability >= .5 for arm in ('baseline', 'unfiltered', *THRESHOLDS)}
            independent = [candidate for candidate in regions[uri]['candidates'] if candidate['independent']]
            candidates = sorted(independent, key=lambda c: -c['learned_score'])[:3]
            output_scores = []
            for candidate in candidates:
                box = candidate['box']
                sample = torch.from_numpy(crop_array(gray, box))[None, None].cuda().float() / 255
                with torch.autocast('cuda', dtype=torch.float16):
                    logits = model(sample)
                patch = central_mask(logits, box, candidate['center'])
                if not separate_mask(masks['unfiltered'], patch, box):
                    continue
                score = float(classifier(sample).sigmoid()[0])
                if not np.isfinite(score):
                    raise ValueError('候选分类分数非有限')
                x0, y0, x1, y1 = box
                masks['unfiltered'][y0:y1, x0:x1] |= patch
                for arm, threshold in THRESHOLDS.items():
                    if score >= threshold:
                        masks[arm][y0:y1, x0:x1] |= patch
                output_scores.append({'box': box, 'center': candidate['center'], 'classification_score': score})
            scores = {'uri': uri, 'added_candidates': output_scores, 'arms': {}}
            for arm, mask in masks.items():
                scores['arms'][arm] = {'pixels': binary_segmentation(mask, reference > 0),
                                       'objects': native_objects(mask, reference)}
            rows.append(scores)
            if index % 32 == 0 or index == len(data):
                atomic_json(output / 'status.json', {'state': 'reviewing', 'images': index, 'total': len(data),
                                                    'resources': resource.sample()})
    return rows


def run(study, review, diagnostic, output):
    study, review, diagnostic = (Path(path).resolve() for path in (study, review, diagnostic))
    origin, identity, audit = open_study(study)
    output = output_path(output, origin['dataset'])
    if (any(output.is_relative_to(path) for path in (study, review, diagnostic)) or (output / 'summary.json').exists()):
        raise ValueError('需要新的独立复核目录')
    previous = read(review / 'summary.json')
    path = study / 'run/last.pt'
    saved = load_checkpoint(path)
    if (
        previous['state'] != 'completed' or previous['test_accessed'] or previous['protocol_sha256'] != identity['protocol_sha256']
        or previous['checkpoint_sha256'] != digest(path) or previous['rows_sha256'] != digest(review / 'images.json')
        or saved['identity'] != identity or saved['epoch'] != origin['budget']
    ):
        raise ValueError('局部掩膜模型或复算指纹不符')
    index_path = diagnostic / 'case-index.json'
    if (read(audit / 'protocol.json')['checkpoint_index_sha256'] != digest(index_path)
            or read(audit / 'summary.json')['rows_sha256'] != digest(audit / 'images.json')):
        raise ValueError('旧模型索引与粗候选指纹不符')
    data = SegmentationDataset(origin['dataset'], 'AR', 'validation')
    frozen = {row['uri']: row for row in read(index_path)
              if row['run_id'] == MODEL and row['diagnostic_subset'] == 'validation'}
    regions = {row['uri']: row for row in read(audit / 'images.json')}
    if len(data) != 276 or set(regions) != set(frozen) or set(regions) != {row['uri'] for row in data.rows}:
        raise ValueError('需复算同一批完整开发图')
    protocol = {
        'refiner_summary_sha256': digest(review / 'summary.json'), 'refiner_pt_sha256': previous['checkpoint_sha256'],
        'code_sha256': digest(__file__), 'cache_sha256': origin['cache_sha256'], 'thresholds': THRESHOLDS,
        'training_examples': 4950, 'training_positive': 1134, 'budget': BUDGET, 'seed': SEED,
        'change': 'Classify object presence separately, then suppress only added masks. Untuned class scores.',
        'development_gate': {'precision_drop': .01, 'recall_gain': .03},
        'scope': 'Reference presence within candidate crop is a noisy proxy. No test or release.',
    }
    output.mkdir(parents=True, exist_ok=True)
    protocol_path = output / 'protocol.json'
    if protocol_path.exists() and read(protocol_path) != protocol:
        raise ValueError('不可混用候选复核版本')
    if not protocol_path.exists():
        atomic_json(protocol_path, protocol)
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        resource = Resources()
        resource.sample()
        classifier = train_presence(study, output, resource, digest(protocol_path))
        if classifier is None:
            return {'state': 'paused'}
        model = UNet(width=8).cuda().eval()
        model.load_state_dict(saved['model'])
        rows = reproduce(model, classifier, data, regions, frozen, diagnostic, output, resource)
        resources = resource.sample()
    results = {arm: estimate(rows, arm) for arm in ('baseline', 'unfiltered', *THRESHOLDS)}
    if results['baseline'] != previous['baseline'] or results['unfiltered'] != previous['refined']:
        raise ValueError('冻结原模型或未筛选的对照未能复算')
    checks = {}
    for arm in THRESHOLDS:
        measured, baseline = results[arm], results['baseline']
        checks[arm] = {
            'recall_gain': measured['region_recall'] - baseline['region_recall'],
            'precision_delta': measured['region_precision'] - baseline['region_precision'],
            'added_unmatched_regions': measured['false_positive_regions'] - baseline['false_positive_regions'],
            'additional_matches': measured['matched_regions'] - baseline['matched_regions'],
        }
    atomic_json(output / 'images.json', rows)
    report = {
        'state': 'completed', 'created_utc': now(), 'images': len(rows), 'results': results, 'checks': checks,
        'practical_gates': {arm: values['recall_gain'] >= .03 and values['precision_delta'] >= -.01
                            for arm, values in checks.items()},
        'classifier_sha256': digest(output / 'last.pt'), 'rows_sha256': digest(output / 'images.json'),
        'protocol_sha256': digest(protocol_path), 'resources': resources, 'test_accessed': False,
        'calibration_accessed': False, 'released': False,
    }
    atomic_json(output / 'summary.json', report)
    atomic_json(output / 'status.json', {'state': 'completed', 'epoch': BUDGET, 'images': len(rows)})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('study', 'review', 'diagnostic', 'output'):
        parser.add_argument('--' + key, required=True)
    args = parser.parse_args()
    configure_environment()
    result = run(**vars(args))
    print(json.dumps({key: result[key] for key in ('state', 'checks', 'practical_gates') if key in result}))


if __name__ == '__main__':
    main()
