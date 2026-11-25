"""Compare native H-alpha candidate recognition with hand-measured features."""

from __future__ import annotations

import argparse
from collections import Counter
from io import BytesIO
from itertools import groupby
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from scipy.ndimage import find_objects, label
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
import torch
from torch import nn
from torch.nn import functional as F

from .ar_hmi_temporal_ranker import matching, object_scores
from .durable import atomic_json, commit
from .fl_author_model import AuthorGraph
from .fl_contour_constraints import replay
from .fl_contour_refiner import scene, supervising_candidates
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT
from .fl_gong_fused_verify import later_pairs
from .fl_gong_patch_training import image_bytes
from .fl_halpha_limb_replay import local_features
from .fl_magfilo_audit import DATA_SHA256, raster
from .fl_magfilo_transfer import LABEL_SHA256, inventory, load_gray
from .fl_spine_context_study import architecture
from .journal import read
from .models import UNet
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


SIZE, BATCH, EPOCHS, SEED = 96, 48, 18, 2026
POSITIVE_IOU = .15


def crop(native, part, box):
    y, x = box
    side = min(2048, max(256, 4 * max(y.stop - y.start, x.stop - x.start) + 64))
    y0 = int(np.clip(2 * (y.start + y.stop) - side // 2, 0, 2048 - side))
    x0 = int(np.clip(2 * (x.start + x.stop) - side // 2, 0, 2048 - side))
    pixels = native[y0:y0 + side, x0:x0 + side]
    # Sample the 512-grid label precisely on the corresponding native-pixel centers.
    oy = (np.arange(y0, y0 + side) // 4)[:, None]
    ox = (np.arange(x0, x0 + side) // 4)[None, :]
    seed = part[oy, ox].astype(np.float32)
    smooth = cv2.GaussianBlur(pixels, (0, 0), 8)
    result = [2 * pixels - 1, np.clip((smooth - pixels) * 12, -2, 2), seed]
    return np.stack([cv2.resize(x, (SIZE, SIZE), interpolation=cv2.INTER_AREA) for x in result]).astype(np.float16)


class ImageReviewer(nn.Module):
    def __init__(self):
        super().__init__()
        encoders = []
        last = 3
        for width in (16, 32, 48, 64):
            encoders.extend((nn.Conv2d(last, width, 3, stride=2, padding=1), nn.GroupNorm(4, width), nn.SiLU()))
            last = width
        self.encoder = nn.Sequential(*encoders)
        self.score = nn.Linear(128, 1)

    def forward(self, image):
        feature = self.encoder(image)
        support = F.adaptive_avg_pool2d(image[:, 2:3], feature.shape[-2:])
        near = F.max_pool2d(support, 3, stride=1, padding=1)
        local = (feature * near).sum((2, 3)) / near.sum((2, 3)).clamp_min(.05)
        return self.score(torch.cat((feature.mean((2, 3)), local), dim=1)).squeeze(1)


def original_image(raw):
    with Image.open(BytesIO(raw)) as pic:
        if pic.size != (2048, 2048):
            raise ValueError('候选分类需要原 2048 Hα JPEG')
        return np.asarray(pic.convert('L'), dtype=np.float32) / 255


def selected_ids(objects, count, refs, features):
    _, negative, matches, _ = supervising_candidates(objects, count, refs)
    proof = np.stack([m[1] for m in matches])
    positive = np.flatnonzero((proof >= POSITIVE_IOU).all(axis=0))
    # Keep positives supported by every available separate annotation.
    budget = max(6, 3 * len(positive))
    sizes = np.bincount(objects.ravel(), minlength=count + 1)[1:]
    ranks = sorted(negative, key=lambda i: (-features[i][3], -int(sizes[i]), int(i)))
    return [(int(i), 1) for i in positive] + [(int(i), 0) for i in ranks[:budget]]


def fit_material(raw, refs, whole, axial):
    _, _, _, radius, mask, n = scene(raw, whole, axial)
    boxes = find_objects(mask)
    source = original_image(raw)
    shape = local_features(mask, load_gray(raw), radius)
    items = []
    for index, truth in selected_ids(mask, n, refs, shape):
        box = boxes[index]
        seed = mask == index + 1
        items.append((crop(source, seed, box), [*shape[index], float(np.log1p(seed.sum()))], truth))
    return items


def learn(items, output, protocol):
    x, descriptors, truth = zip(*items)
    x, y = np.stack(x), np.asarray(truth, dtype=np.float32)
    if min(int(y.sum()), len(y) - int(y.sum())) < 24:
        raise ValueError('在早期图中支持分类的候选不足')
    tabular = make_pipeline(StandardScaler(), LogisticRegression(class_weight='balanced', max_iter=400))
    tabular.fit(descriptors, y)
    standard = tabular['standardscaler']
    logistic = tabular['logisticregression']
    atomic_json(output / 'feature-reviewer.json', {'mean': standard.mean_.tolist(), 'scale': standard.scale_.tolist(),
                'coef': logistic.coef_[0].tolist(), 'intercept': float(logistic.intercept_[0]),
                'protocol_sha256': protocol})
    torch.manual_seed(SEED)
    model = ImageReviewer().cuda()
    optim = torch.optim.AdamW(model.parameters(), lr=.0007)
    positive_weight = torch.tensor((len(y) - y.sum()) / y.sum(), device='cuda', dtype=torch.float32)
    epochs = []
    for epoch in range(EPOCHS):
        indices = np.random.default_rng(SEED + epoch).permutation(len(x))
        losses = []
        for start in range(0, len(indices), BATCH):
            subset = indices[start:start + BATCH]
            inputs = torch.as_tensor(x[subset], device='cuda', dtype=torch.float32)
            target = torch.as_tensor(y[subset], device='cuda')
            loss = F.binary_cross_entropy_with_logits(model(inputs), target, pos_weight=positive_weight)
            if not torch.isfinite(loss):
                raise ValueError('候选判别损失非有限')
            optim.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 4, error_if_nonfinite=True)
            optim.step()
            losses.append(float(loss.detach()))
        current = {'model': model.state_dict(), 'optimizer': optim.state_dict(), 'epoch': epoch + 1,
                   'protocol_sha256': protocol}
        commit(output / 'reviewer.pt', lambda f: torch.save(current, f))
        epochs.append(float(np.mean(losses)))
        atomic_json(output / 'status.json', {'state': 'fit_reviewer', 'epoch': epoch + 1, 'loss': epochs[-1]})
        if (output/'PAUSE').exists():
            return None
    atomic_json(output / 'fit-metrics.json', {'training_candidates': len(y), 'positive': int(y.sum()),
                                             'proxy_background': len(y) - int(y.sum()), 'epoch_loss': epochs})
    return model.eval(), tabular


def gather(raw, refs, whole, axial, refiner, reviewer, tabular):
    masks, radius, seeds = replay(raw, whole, axial, refiner)
    base, old_count = label(masks['baseline'], structure=CONNECTIVITY)
    regions, count = label(masks['bounded_dark'] & ~masks['baseline'], structure=CONNECTIVITY)
    joined = np.where(regions > 0, old_count + regions, base)
    geometry = [object_scores(joined, [{'area': int(n)} for n in np.bincount(joined.ravel())[1:]],
                              label(ref, structure=CONNECTIVITY)[0], [True] * (old_count + count))[1] for ref in refs]
    scores = {'visual': [], 'features': []}
    if count:
        original = original_image(raw)
        shape = local_features(seeds, load_gray(raw), radius)
        descriptors, patches = [], []
        for ident, bbox in enumerate(find_objects(regions), 1):
            overlaps = np.bincount(seeds[regions == ident], minlength=int(seeds.max()) + 1)
            overlaps[0] = 0
            seed = int(overlaps.argmax())
            if seed == 0:
                raise RuntimeError('新增区域脱离原始候选')
            part = seeds == seed
            descriptors.append([*shape[seed - 1], float(np.log1p(part.sum()))])
            patches.append(crop(original, part, bbox))
        scores['features'] = tabular.decision_function(descriptors).tolist()
        for start in range(0, len(patches), BATCH):
            images = torch.as_tensor(np.stack(patches[start:start+BATCH]), device='cuda', dtype=torch.float32)
            scores['visual'].extend(reviewer(images).cpu().tolist())
    return {'base_count': old_count, 'count': count, 'scores': scores, 'geometry': geometry}


def measure(rows, arm, cutoff):
    stats = Counter(matched=0, reference=0, predicted=0)
    for row in rows:
        n, m = row['base_count'], row['count']
        keep = [True] * n + ([True] * m if arm == 'all' else [False] * m if arm == 'whole'
                             else [bool(value >= cutoff) for value in row['scores'][arm]])
        for geometry in row['geometry']:
            found, _ = matching(geometry, keep)
            stats.update(matched=len(found), reference=len(geometry['reference_ids']), predicted=sum(keep))
    return {**stats, 'unmatched': stats['predicted'] - stats['matched'],
            'precision': stats['matched'] / max(1, stats['predicted']), 'recall': stats['matched'] / max(1, stats['reference'])}


def exact_policy(rows):
    """Sweep every distinct score by recomputing only scenes whose selection changed."""
    base = [measure([row], 'whole', 0.) for row in rows]
    initial_matches = sum(r['matched'] for r in base)
    initial_unmatched = sum(r['unmatched'] for r in base)
    solutions = {}
    for arm in ('visual', 'features'):
        events = sorted((value, i) for i, row in enumerate(rows) for value in row['scores'][arm])
        counts = base.copy()
        gain = extra = 0
        accepted, diagnostic = [], []
        for threshold, changed in groupby(reversed(events), key=lambda event: event[0]):
            if not np.isfinite(threshold):
                raise ValueError('候选判别分数非有限')
            for index in {j for _, j in changed}:
                revised = measure([rows[index]], arm, threshold)
                gain += revised['matched'] - counts[index]['matched']
                extra += revised['unmatched'] - counts[index]['unmatched']
                counts[index] = revised
            if gain >= 3:
                point = (-extra, gain, float(threshold))
                diagnostic.append(point)
                if extra <= gain:
                    accepted.append(point)
        point = max(accepted or diagnostic, default=None)
        if point:
            value = measure(rows, arm, point[2])
            if value['matched'] != initial_matches + point[1] or value['unmatched'] != initial_unmatched - point[0]:
                raise RuntimeError('完整门槛搜索与逐图复算不一致')
        solutions[arm] = {'passed': bool(accepted), 'gain': point[1] if point else 0, 'cutoff': point[2] if point else None,
                          'extra_unmatched': -point[0] if point else None, 'distinct_scores': len({v for v, _ in events})}
    return solutions


def run(source, release, early, development, later, base, spine, refiner, previous, output):
    output = output_path(output, DATASET)
    if output.exists() or digest(source) != DATA_SHA256 or digest(release) != LABEL_SHA256:
        raise ValueError('候选判别须使用冻结标注和独立输出路径')
    old = read(previous / 'summary.json')
    old_plan = read(previous / 'protocol.json')
    weights = [base/'mixed_gong/last.pt', spine/'mask_and_spine/last.pt', refiner/'last.pt']
    fit_dates = read(refiner/'protocol.json')
    broad = read(refiner.parent/'fl-halpha-limb-replay-01/protocol.json')['samples']['fit']
    if (old['test_accessed'] or digest(previous/'protocol.json') != old['protocol_sha256']
            or [digest(p) for p in weights] != old_plan['weight_sha256']
            or set(broad) & set(old_plan['samples']['development'] + old_plan['samples']['2019_2020'])
            or not all(v[:4] < '2017' for v in broad)
            or not set(fit_dates['samples']['fit']) <= set(broad)):
        raise ValueError('模型或时间隔离清单不符')
    _, annotations = inventory(source, release)
    _, archived = later_pairs(source, release)
    cohorts = {'fit': (early, broad, annotations), 'development': (development, old_plan['samples']['development'], annotations),
               '2019_2020': (later, old_plan['samples']['2019_2020'], archived)}
    atomic_json(output/'protocol.json', {'code_sha256': digest(__file__), 'old_sha256': digest(previous/'summary.json'),
        'weight_sha256': [digest(p) for p in weights], 'samples': {k: v[1] for k, v in cohorts.items()},
        'positive_iou_all_annotations': POSITIVE_IOU, 'epochs': EPOCHS,
        'negatives': 'proxy background; zero overlap in every same-observation annotation, dark and large sampled first',
        'criterion': 'development strict gain >=3 and additional unmatched <= gain; held dates never select',
        'test_accessed': False, 'fresh_years_accessed': False})
    whole, axial, refine = AuthorGraph('batch'), architecture(), UNet(width=8)
    refine.encoders[0].layers[0] = nn.Conv2d(5, 8, 3, padding=1)
    whole.load_state_dict(load_checkpoint(weights[0])['model'])
    for model, path in ((axial, weights[1]), (refine, weights[2])):
        model.load_state_dict(torch.load(path, map_location='cpu', weights_only=True)['model'])
    cv2.setNumThreads(2)
    torch.set_num_threads(2)
    reports = {}
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        for model in (whole, axial, refine):
            model.cuda().eval()
        usage = Resources()
        for phase, (folder, names, annotation) in cohorts.items():
            rows = {r['file']: r for r in read(folder/'inventory.json')}
            data = []
            for i, name in enumerate(names, 1):
                if (output/'PAUSE').exists():
                    return {'state': 'paused', 'phase': phase, 'done': i - 1}
                row = rows[name]
                raw = image_bytes(folder, row)
                refs = [annotation[a['id']] for a in row['annotators']]
                refs = [raster(r.values() if isinstance(r, dict) else r) for r in refs]
                with torch.inference_mode():
                    if phase == 'fit':
                        data.extend(fit_material(raw, refs, whole, axial))
                    else:
                        data.append({'file': name, **gather(raw, refs, whole, axial, refine, visual, tabular)})
                if i % 12 == 0:
                    atomic_json(output/'status.json', {'state': phase, 'done': i, 'total': len(names), 'resources': usage.sample()})
            if phase == 'fit':
                training = learn(data, output, digest(output/'protocol.json'))
                if training is None:
                    return {'state': 'paused', 'phase': 'fit'}
                visual, tabular = training
                continue
            atomic_json(output/f'{phase}-cases.json', data)
            if phase == 'development':
                selected = exact_policy(data)
                atomic_json(output/'policy.json', selected)
            reports[phase] = {k: measure(data, k, 0.) for k in ('whole', 'all')}
            expected = old['scores'][phase]
            for k, base_arm in (('whole', 'baseline'), ('all', 'bounded_dark')):
                if reports[phase][k]['matched'] != expected[base_arm]['matched_iou_050']:
                    raise ValueError('固定对照的严格匹配未复现')
            for arm, choice in selected.items():
                if choice['cutoff'] is not None:
                    reports[phase][arm] = {**measure(data, arm, choice['cutoff']), 'policy_passed': choice['passed']}
        resources = usage.sample()
    summary = {'state': 'completed', 'created_utc': now(), 'policy': selected, 'scores': reports, 'resources': resources,
               'cases_sha256': {k: digest(output/f'{k}-cases.json') for k in reports},
               'checkpoint_sha256': digest(output/'reviewer.pt'), 'protocol_sha256': digest(output/'protocol.json'),
               'test_accessed': False, 'deployed': False}
    atomic_json(output/'summary.json', summary)
    atomic_json(output/'status.json', {'state': 'completed', 'summary_sha256': digest(output/'summary.json')})
    return summary


if __name__ == '__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('source', 'release', 'early', 'development', 'later', 'base', 'spine', 'refiner', 'previous', 'output'):
        parser.add_argument('--' + key, type=Path, required=True)
    result = run(**vars(parser.parse_args()))
    print(json.dumps({'state': result['state'], 'policy': result.get('policy')}, ensure_ascii=False))
