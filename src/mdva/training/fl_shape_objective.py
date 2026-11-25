"""Compare boundary and centerline supervision on the frozen H-alpha split."""

from __future__ import annotations

import argparse
from collections import defaultdict
from hashlib import sha256
from pathlib import Path

import cv2
import numpy as np
from scipy.ndimage import label
import torch
from torch.nn import functional as F

from .durable import atomic_json, commit
from .fl_direct_centers import BATCH, EPOCHS, SEED, architecture, fitting_tiles, infer, input_planes, objective
from .fl_direct_reviewer import evidence
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT
from .fl_magfilo_audit import DATA_SHA256, raster
from .fl_visual_candidate_review import measure
from .journal import read
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


ARMS = ('edge', 'edge_centerline')
PAPER = 'https://openaccess.thecvf.com/content/CVPR2021/html/Shit_clDice_-_A_Novel_Topology-Preserving_Loss_Function_for_Tubular_Structure_Segmentation_CVPR_2021_paper.html'


def erode(image):
    return torch.minimum(-F.max_pool2d(-image, (3, 1), stride=1, padding=(1, 0)),
                         -F.max_pool2d(-image, (1, 3), stride=1, padding=(0, 1)))


def skeleton(image, steps=5):
    points = torch.zeros_like(image)
    for _ in range(steps):
        smaller = erode(image)
        opened = F.max_pool2d(smaller, 3, stride=1, padding=1)
        tips = (image - opened).relu()
        points = points + (tips - tips * points).relu()
        image = smaller
    return points


def shape_terms(logits, label_votes, valid, include_centerline):
    mask = logits[:, None]
    domain = valid[:, None]
    votes = label_votes[:, None]
    certain = (votes == 1) & domain
    background = (votes == 0) & domain
    target = certain.float()
    core = -F.max_pool2d(-target, 5, stride=1, padding=2)
    rim_in = certain & (core < 1)
    rim_out = background & (F.max_pool2d(target, 5, stride=1, padding=2) > 0)
    inside = -(F.logsigmoid(mask) * rim_in).sum() / rim_in.sum().clamp_min(1)
    outside = -(F.logsigmoid(-mask) * rim_out).sum() / rim_out.sum().clamp_min(1)
    boundary = .5 * (inside + outside)
    if not include_centerline:
        return boundary, mask.new_zeros(())
    with torch.no_grad():
        reference_spine = skeleton(target)
    predicted = mask.sigmoid() * domain
    predicted_spine = skeleton(predicted)
    axes = (1, 2, 3)
    support = reference_spine.sum(axes)
    recall = ((reference_spine * predicted).sum(axes) + 1e-3) / (support + 1e-3)
    # A disputed pixel is neutral for the precision term.
    known_spine = predicted_spine * (certain | background)
    precision = ((known_spine * target).sum(axes) + 1e-3) / (known_spine.sum(axes) + 1e-3)
    distance = 1 - 2 * precision * recall / (precision + recall).clamp_min(1e-6)
    return boundary, (distance * (support > 0)).sum() / (support > 0).sum().clamp_min(1)


def train(data, arm, output, monitor, identity):
    x, y, support = data
    torch.manual_seed(SEED)
    model = architecture().cuda()
    solver = torch.optim.AdamW(model.parameters(), lr=.001)
    filename = output / arm / 'last.pt'
    history = []
    if filename.exists():
        state = torch.load(filename, map_location='cpu', weights_only=True)
        if state['identity'] != [identity, arm]:
            raise ValueError('恢复检查点身份不符')
        solver.load_state_dict(state['optimizer'])
        model.load_state_dict(state['model'])
        history = state['history']
    for epoch in range(len(history), EPOCHS):
        if (output / 'PAUSE').exists():
            return None
        order = np.random.default_rng(SEED+epoch).permutation(len(x))
        metrics = []
        for start in range(0, len(x), BATCH):
            idx = order[start:start+BATCH]
            inputs = torch.as_tensor(x[idx], dtype=torch.float32, device='cuda')
            targets = torch.as_tensor(y[idx], dtype=torch.float32, device='cuda')
            valid = torch.as_tensor(support[idx], device='cuda')
            solver.zero_grad(set_to_none=True)
            pred = model(inputs)
            base = objective(pred, targets, valid)
            boundary, spine = shape_terms(pred[:, 0], targets[:, 0], valid, arm == 'edge_centerline')
            loss = base + .25 * boundary + .20 * spine
            if not bool(torch.isfinite(loss)):
                raise ValueError('暗条损失出现非有限值')
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 4., error_if_nonfinite=True)
            solver.step()
            metrics.append(torch.stack((base, boundary, spine)).detach())
        history.append(torch.stack(metrics).mean(0).tolist())
        checkpoint = {'identity': [identity, arm], 'model': model.state_dict(),
                      'optimizer': solver.state_dict(), 'history': history}
        commit(filename, lambda stream: torch.save(checkpoint, stream))
        atomic_json(output/'status.json', {'state': 'training', 'arm': arm, 'epoch': epoch+1, 'total': EPOCHS,
                                           'loss_terms': history[-1], 'resources': monitor.sample()})
    return model.eval()


def plan_records(source, study, names):
    sparse = study/'fl-sparse-reviewer-02'
    sources = {'fit': (source/'train-pool-01', 'inventory.json'),
        'development': (source/'transfer-dev-01', 'inventory.json'),
        '2019_2020': (study/'fl-gong-fused-2019-2020-01', 'inventory.json'),
        'new_2018': (sparse, 'download.json'),
        'sparse_replay': (source/'archive-fresh-2019-2020-01', 'image-label-identity.json')}
    cohorts, inputs = {}, {}
    for phase, (folder, filename) in sources.items():
        collection = read(folder/filename)
        inputs[phase] = digest(folder/filename)
        if isinstance(collection, dict):
            collection = collection['files']
        by_name = {row['file']: row for row in collection}
        cohorts[phase] = (folder, [by_name[name] for name in names[phase]])
    return cohorts, inputs


def item(root, record, labels, names):
    name = record['file']
    raw = (root/'jpeg'/name).read_bytes()
    expected = record.get('image_sha256') or record.get('jpeg_sha256') or record.get('sha256')
    if sha256(raw).hexdigest() != expected:
        raise ValueError('Hα图像指纹不符')
    ids = record['variants'] if 'variants' in record else [r['id'] for r in record['annotators']]
    if any(names[id] != name for id in ids):
        raise ValueError('Hα参考身份不符')
    return raw, [labels[id] for id in ids]


def run(source, study, output):
    output = output_path(output, DATASET)
    original = source/'magfilo_2024_v1.0.json'
    detector = study/'fl-direct-centers-01'
    baseline = read(detector/'summary.json')
    replay_dir = study/'fl-shape-replay-01'
    replay = read(replay_dir/'summary.json')
    scheme = read(replay_dir/'protocol.json')
    weights = detector/'direct.pt'
    if ((output/'summary.json').exists() or digest(original) != DATA_SHA256
            or baseline['test_accessed'] or replay['test_accessed']
            or digest(weights) != baseline['model_sha256'] or digest(weights) != scheme['detector_sha256']
            or digest(detector/'protocol.json') != baseline['protocol_sha256']
            or digest(replay_dir/'protocol.json') != replay['protocol_sha256']):
        raise ValueError('需要固定对照身份及无完成结果的实验目录')
    names = {'fit': read(detector/'protocol.json')['samples']['fit'], **scheme['samples']}
    cohorts, inputs = plan_records(source, study, names)
    protocol = {'code_sha256': digest(__file__), 'base_summary_sha256': digest(detector/'summary.json'),
        'reference_sha256': DATA_SHA256, 'inputs': inputs, 'samples': names, 'threshold': .5,
        'arms': list(ARMS), 'epochs': EPOCHS, 'seed': SEED, 'method_source': PAPER,
        'loss': 'same binary/center objective, +0.25 certain-boundary BCE, optionally +0.20 soft-clDice-derived term',
        'test_accessed': False}
    existing = read(output/'protocol.json')
    if existing is None:
        atomic_json(output/'protocol.json', protocol)
    elif protocol != existing:
        raise ValueError('该实验目录的协议已改变')
    identity = digest(output/'protocol.json')
    original = read(original)
    annotation_names = {r['id']: r['file_name'] for r in original['images']}
    labels = defaultdict(list)
    for row in original['annotations']:
        labels[row['image_id']].append(row)
    cv2.setNumThreads(2)
    torch.set_num_threads(2)
    samples = []
    folder, items = cohorts.pop('fit')
    for i, record in enumerate(items, 1):
        raw, refs = item(folder, record, labels, annotation_names)
        fields, valid, _, _ = input_planes(raw)
        samples.extend(fitting_tiles(fields, valid, refs, np.random.default_rng(SEED+i)))
    x, y, support, counts = zip(*samples, strict=True)
    tensors = tuple(map(np.stack, (x, y, support)))
    del x, y, support, samples
    fitting = {'tiles': len(counts), 'tiles_with_centers': sum(bool(c) for c in counts),
               'centers_across_tiles': sum(counts)}
    if fitting != read(detector/'fitting.json'):
        raise ValueError('训练对照的图块清单未复现')
    atomic_json(output/'fitting.json', fitting)
    control = architecture()
    control.load_state_dict(torch.load(weights, map_location='cpu', weights_only=True)['model'])
    models = {}
    results = {}
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        monitor = Resources()
        for arm in ARMS:
            model = train(tensors, arm, output, monitor, identity)
            if model is None:
                atomic_json(output/'status.json', {'state':'paused', 'arm':arm})
                return
            models[arm] = model
        del tensors
        models['control'] = control.cuda().eval()
        with torch.inference_mode():
            for phase, (folder, records) in cohorts.items():
                rows = []
                for record in records:
                    raw, variants = item(folder, record, labels, annotation_names)
                    fields, valid, _, _ = input_planes(raw)
                    refs = [label(raster(v), structure=CONNECTIVITY)[0] for v in variants]
                    row = {}
                    for arm, model in models.items():
                        mask = infer(model, fields, valid)[0] >= .5
                        count = label(mask, structure=CONNECTIVITY)[1]
                        row[arm] = {'base_count':0, 'count':count, 'geometry':evidence(mask, refs)}
                    rows.append({'file':record['file'], 'arms':row})
                    monitor.sample()
                results[phase] = {arm: measure([r['arms'][arm] for r in rows], 'all', 0.) for arm in models}
                if results[phase]['control'] != replay['scores'][phase]['scores']['baseline']:
                    raise ValueError('冻结暗条基线未复现')
                atomic_json(output/f'{phase}-cases.json', rows)
        resources = monitor.sample()
    atomic_json(output/'summary.json', {'state':'completed', 'recorded_utc':now(),
        'results':results, 'resources':resources, 'protocol_sha256':identity, 'test_accessed':False,
        'deployed':False, 'checkpoints':{a:digest(output/a/'last.pt') for a in ARMS}})
    atomic_json(output/'status.json', {'state':'completed', 'summary_sha256':digest(output/'summary.json')})


if __name__ == '__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'study', 'output'):
        parser.add_argument('--'+name, required=True, type=Path)
    run(**vars(parser.parse_args()))
