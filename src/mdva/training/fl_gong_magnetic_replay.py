"""Evaluate fixed LOS corroboration for H-alpha spine proposals against controls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from scipy.ndimage import distance_transform_edt, label
import torch

from .durable import atomic_json, commit
from .fl_author_model import AuthorGraph
from .fl_candidate_review_2021 import publicly_paired
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT, overlap_profile
from .fl_gong_hmi_historical import NEAR_PIL_PIXELS
from .fl_gong_resolution import predict
from .fl_magfilo_audit import DATA_SHA256, compare, raster
from .fl_magfilo_transfer import LABEL_SHA256, load_gray, metrics
from .fl_spine_context_study import EPOCHS, architecture, context, full_score, locate
from .journal import read
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


FRACTION = .5


def supported(proposals, valid, transition):
    labels, _ = label(proposals, structure=CONNECTIVITY)
    sizes = np.bincount(labels.ravel())
    near = distance_transform_edt(~transition) <= NEAR_PIL_PIXELS if transition.any() else transition
    n_supported = np.bincount(labels[near & valid], minlength=len(sizes))
    n_outside = np.bincount(labels[~valid], minlength=len(sizes))
    keep = (n_supported >= FRACTION * sizes) & (n_outside == 0)
    keep[0] = False
    return keep[labels]


def evaluation(raw, whole, spine, valid, transition, references, prior):
    baseline = predict(whole, load_gray(raw)[None])[0] >= .5
    lobes, _ = label(baseline, structure=CONNECTIVITY)
    count = int(lobes.max())
    for mask, recorded in zip(references, prior['review']['original'], strict=True):
        calculated = overlap_profile(baseline, label(mask, structure=CONNECTIVITY)[0])
        if count != len(prior['features']) or calculated['matched_iou_050'] != recorded['matched']:
            raise ValueError('冻结基线区域数或匹配未复现')
    planes, disk = context(raw)
    contour, axial = full_score(spine, planes, disk)
    proposals = locate(contour, axial, baseline)
    supported_real = supported(proposals, valid, transition)
    supported_mirror = supported(proposals, valid[:, ::-1], transition[:, ::-1])
    masks = {'whole': baseline, 'all_spine': baseline | proposals, 'actual_los': baseline | supported_real,
             'mirrored_los': baseline | supported_mirror}
    results = {}
    for key, mask in masks.items():
        results[key] = [{**compare(mask, ref), **overlap_profile(mask, label(ref, structure=CONNECTIVITY)[0])}
                        for ref in references]
    return results, {'baseline': baseline, 'spine_proposals': proposals, 'supported_los': supported_real,
                     'supported_mirror': supported_mirror}


def run(observations, verified, spine_study, source, release, output):
    output = output_path(output, DATASET)
    if output.exists() or any(output.is_relative_to(p.resolve()) for p in (observations, verified, spine_study)):
        raise ValueError('复算使用独立新目录')
    observed, baseline, spine = (read(p / 'summary.json') for p in (observations, verified, spine_study))
    whole_path = verified / baseline['candidate']
    spine_path = spine_study / 'mask_and_spine' / 'last.pt'
    if (any(s['state'] != 'completed' or s['test_accessed'] for s in (observed, baseline, spine))
            or digest(observations / 'cases.json') != observed['cases_sha256']
            or digest(verified / 'external-cases.json') != baseline['cases_sha256']
            or digest(whole_path) != baseline['candidate_sha256']
            or digest(spine_study / 'protocol.json') != spine['protocol_sha256']
            or digest(source) != DATA_SHA256 or digest(release) != LABEL_SHA256):
        raise ValueError('旧模型、观测或标注摘要不符')
    rows = [r for r in read(observations / 'cases.json') if r['state'] == 'angular_comparison_available']
    checkpoints = torch.load(spine_path, map_location='cpu', weights_only=True)
    if checkpoints['identity'] != [spine['protocol_sha256'], 'mask_and_spine'] or len(checkpoints['history']) != EPOCHS:
        raise ValueError('主轴检查点身份或训练预算不符')
    protocol = {'code_sha256': digest(__file__), 'cohort_sha256': digest(observations / 'summary.json'),
                'base_weight_sha256': digest(whole_path), 'spine_checkpoint_sha256': digest(spine_path),
                'near_distance_512px': NEAR_PIL_PIXELS, 'min_candidate_fraction_near': FRACTION,
                'arms': ['whole', 'all_spine', 'actual_los', 'mirrored_los'],
                'scope': '2021 known diagnostic observations, exploratory same-sample paired replay',
                'files': [r['image'] for r in rows], 'issued_forecast': False, 'test_accessed': False}
    if not rows:
        raise ValueError('未取得满足近时、方向及质量门槛的科研图')
    atomic_json(output / 'protocol.json', protocol)
    annotations, polygons = publicly_paired(source, release)
    rows_by_name = {r['file']: r for r in annotations}
    photos = {r['file']: r for r in read(verified / 'inventory.json')}
    priors = {r['name']: r for r in read(verified / 'external-cases.json')}
    cases = []
    cv2.setNumThreads(2)
    torch.set_num_threads(2)
    with exclusive(ROOT / '.mdva/training-gpu.lock'), torch.inference_mode():
        from safetensors.torch import load_file

        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        meter = Resources()
        whole = AuthorGraph('batch').cuda().eval()
        whole.load_state_dict(load_file(str(whole_path)))
        joint = architecture().cuda().eval()
        joint.load_state_dict(checkpoints['model'])
        for row in rows:
            name = row['image']
            path = verified / 'jpeg' / name
            if digest(path) != photos[name]['image_sha256'] or digest(observations / row['asset']) != row['asset_sha256']:
                raise ValueError('图像或配准缓存指纹不符')
            variants = [raster(polygons[r['id']].values()) for r in rows_by_name[name]['annotators']]
            with np.load(observations / row['asset'], allow_pickle=False) as pattern:
                arms, masks = evaluation(path.read_bytes(), whole, joint, pattern['valid'],
                                         pattern['los_sign_transition'], variants, priors[name])
            cache = output / 'masks' / (Path(name).stem + '.npz')
            commit(cache, lambda f: np.savez_compressed(f, **masks))
            cases.append({'image': name, 'sha256': digest(cache), 'assets': cache.relative_to(output).as_posix(),
                          'arms': arms, 'time_offset_seconds': row['signed_time_offset_seconds']})
            meter.sample()
        resources = meter.sample()
    scores = {}
    for group, subset in (('30_min', cases), ('15_min', [r for r in cases if abs(r['time_offset_seconds']) <= 900])):
        scores[group] = {}
        for arm in protocol['arms']:
            samples = [{'comparisons': r['arms'][arm]} for r in subset]
            scores[group][arm] = {**metrics(samples), 'fully_missed_references': sum(
                v['zero_overlap_references'] for r in samples for v in r['comparisons'])}
    atomic_json(output / 'cases.json', cases)
    summary = {'state': 'completed', 'recorded_utc': now(), 'scores': scores, 'resources': resources,
               'protocol_sha256': digest(output / 'protocol.json'), 'cases_sha256': digest(output / 'cases.json'),
               'trained': False, 'test_accessed': False, 'deployed': False}
    atomic_json(output / 'summary.json', summary)
    return summary


if __name__ == '__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('observations', 'verified', 'spine_study', 'source', 'release', 'output'):
        parser.add_argument('--' + name.replace('_', '-'), type=Path, required=True)
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))
