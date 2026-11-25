"""Replay the paired segmentation checkpoint with an independently fitted small-candidate ranker."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import distance_transform_edt, find_objects
import torch
from torch.nn import functional as F

from .ar_continuum_study import expand
from .ar_hmi_candidate_ranker import candidates, decision, policy_split, tally
from .ar_hmi_segmentation import HmiDataset, setup
from .ar_scale_review import native_objects
from .ar_scale_study import ROOT, warm_model
from .ar_native_tiles import add_isolated
from .durable import atomic_json
from .journal import read
from .reviewed_train import load_checkpoint, seed_all
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


def describe_additions(raster, added, reference, parts, params, new_matches):
    identifiers = np.unique(raster[added])
    if not len(identifiers):
        return []
    windows = find_objects(raster)
    numbers, areas = np.unique(reference[reference > 0], return_counts=True)
    reference_areas = dict(zip(numbers.tolist(), areas.tolist(), strict=True))
    distance = None
    records = []
    for identifier in identifiers:
        window = windows[identifier-1]
        own = raster[window] == identifier
        info = parts[identifier-1]
        overlaps, counts = np.unique(reference[window][own], return_counts=True)
        ious = {int(number): int(count)/(info['area']+reference_areas[number]-int(count))
                for number, count in zip(overlaps, counts, strict=True) if number > 0}
        best = max(ious, key=ious.get) if ious else None
        best_iou = ious[best] if best else 0.
        if best is None and reference_areas and distance is None:
            distance = distance_transform_edt(reference == 0)
        classification = ('recovered_reference' if best_iou >= .5 and best in new_matches
                          else 'overlaps_reference' if best is not None else 'disjoint_from_reference')
        y, x = window
        records.append({'component': int(identifier), 'bbox_xyxy': [int(x.start), int(y.start), int(x.stop), int(y.stop)],
                        'classification': classification, 'best_reference': best, 'best_iou': best_iou,
                        'reference_distance_pixels': 0. if best is not None else (
                            float(distance[window][own].min()) if distance is not None else None),
                        'area': info['area'], 'ranker_score': decision(info, params)[0],
                        'mean_pixel_score': info.get('mean_score'), 'minimum_polarity_fraction': info.get('minimum_polarity_fraction'),
                        'absolute_p95_gauss': info.get('absolute_p95_gauss'), 'local_correlation': info.get('local_correlation')})
    return records


def run(study, source, cohort, ranker, output, supplement=False, fresh=None, frozen_supplement=None):
    origin, known, plan, study, cohort = setup(study, source, cohort)
    ranker = Path(ranker).resolve()
    output = output_path(output, origin['dataset'])
    if output.exists() or any(output.is_relative_to(path) for path in (study, cohort, ranker)):
        raise ValueError('分割和复核组合使用独立输出目录')
    comparison, fit = (read(path/'summary.json') for path in (study, ranker))
    if (comparison['test_accessed'] or fit['test_accessed'] or comparison['calibration_accessed'] or fit['calibration_accessed']
            or comparison['protocol_sha256'] != digest(study/'protocol.json') or not fit['small_only']
            or comparison['images_sha256'] != digest(study/'images.json') or fit['rankers_sha256'] != digest(ranker/'rankers.json')
            or read(ranker/'protocol.json')['cohort_summary_sha256'] != digest(cohort/'summary.json')):
        raise ValueError('组合对照来源身份不符')
    replay = {r['uri']: r['arms'] for r in read(study/'images.json')}
    old_ranker = {r['uri']: r['objects']['physical'] for r in read(ranker/'images.json')}
    if fit['images_sha256'] != digest(ranker/'images.json') or set(replay) != set(old_ranker):
        raise ValueError('不同开发样本，不能比较')
    params = read(ranker/'rankers.json')['physical']
    supplement_threshold = None
    if supplement:
        training = ranker/'training-candidates.json'
        if fit['training_candidates_sha256'] != digest(training):
            raise ValueError('补检阈值用的训练候选发生变化')
        _, policy = policy_split(read(training))
        negatives = [decision(row, params)[0] for row in policy if not row['matched_reference']]
        if len(negatives) < 20:
            raise ValueError('较晚的训练负候选不足以设定补检阈值')
        supplement_threshold = float(np.nextafter(np.quantile(negatives, .95, method='higher'), np.inf))
    evaluation_cohort = cohort
    fresh_sha = None
    threshold_evidence = None
    if fresh is not None:
        if not supplement or frozen_supplement is None:
            raise ValueError('新开发复验需要已固定的补检对照')
        evaluation_cohort = Path(fresh).resolve()
        if output.is_relative_to(evaluation_cohort):
            raise ValueError('新开发复验必须使用独立输出目录')
        sample = read(evaluation_cohort/'summary.json')
        sample_plan = read(evaluation_cohort/'protocol.json')
        frozen = read(Path(frozen_supplement))
        if (sample['state'] != 'completed' or sample['test_accessed'] or sample['calibration_accessed']
                or sample['protocol_sha256'] != digest(evaluation_cohort/'protocol.json')
                or sample_plan['previous_summary_sha256'] != digest(cohort/'summary.json')
                or frozen['test_accessed'] or frozen['calibration_accessed']
                or frozen['segmentation_summary_sha256'] != digest(study/'summary.json')
                or frozen['ranker_summary_sha256'] != digest(ranker/'summary.json')
                or frozen['supplement_threshold'] != supplement_threshold):
            raise ValueError('新开发样本和已固定补检参数不符')
        rows_by_uri = {row['uri']: row for row in sample['results']}
        if (not rows_by_uri or len(rows_by_uri) != len(sample['results'])
                or set(rows_by_uri) != set(sample_plan['selection']) or set(rows_by_uri) & set(known['validation'])):
            raise ValueError('新开发复验与冻结的独立登记样本不符')
        known = {**known, 'validation': rows_by_uri}
        fresh_sha = digest(evaluation_cohort/'summary.json')
        threshold_evidence = digest(Path(frozen_supplement))
    checkpoint = study/'hmi/run/last.pt'
    if comparison['checkpoint_sha256']['hmi'] != digest(checkpoint):
        raise ValueError('分割权重改变')
    saved = load_checkpoint(checkpoint)
    if saved['identity']['code'] != plan['code'] or saved['epoch'] != plan['epochs']:
        raise ValueError('分割检查点身份不符')
    models = {'starting': warm_model(origin, plan['config'])[0], 'hmi': expand(warm_model(origin, plan['config'])[0])}
    models['hmi'].load_state_dict(saved['model'])
    dataset = HmiDataset(origin['dataset'], 'validation', evaluation_cohort, known, True)
    rows = []
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        seed_all(2026)
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        resource = Resources()
        for model in models.values():
            model.cuda().eval()
        with torch.inference_mode():
            for index, source_row in enumerate(dataset.rows):
                uri = source_row['uri']
                item = dataset[index]
                reference = np.load(dataset.source(source_row['mask_uri']), allow_pickle=False)
                pair = item['image'][None].cuda()
                record = known['validation'][uri]
                field = (np.load(evaluation_cohort/'pairs'/record['id']/'aligned-los.npy', allow_pickle=False) if item['physical_used'] else None)
                with Image.open(dataset.source(uri)) as image:
                    gray = np.array(image.convert('L'))
                result = {'uri': uri, 'lane': record['lane'], 'objects': {}}
                masks = {}
                for name, model in models.items():
                    with torch.autocast('cuda', dtype=torch.float16):
                        logits = model(pair if name == 'hmi' else pair[:, :1])
                    p = F.interpolate(logits.float().sigmoid(), size=reference.shape, mode='bilinear', align_corners=False)[0, 0].cpu().numpy()
                    base, raster, parts = candidates(p, reference, field, gray)
                    original = native_objects(base, reference)
                    if fresh is None and original != replay[uri][name]['objects']:
                        raise ValueError('分割候选逐图重放不一致')
                    keep = [False, *(r['area'] >= 512 or decision(r, params)[1] for r in parts)]
                    masks[name] = np.asarray(keep)[raster]
                    result['objects'][name] = original
                    result['objects'][name+'_reviewed'] = native_objects(masks[name], reference)
                    if name == 'hmi' and supplement:
                        selected = [False]
                        for part in parts:
                            score, _ = decision(part, params) if part['area'] < 512 else (None, False)
                            selected.append(score is not None and score >= supplement_threshold)
                        additions = np.asarray(selected)[raster]
                        merged = add_isolated(masks['starting'], additions)
                        final = native_objects(merged, reference)
                        result['objects']['starting_with_supplement'] = final
                        new_ids = set(final['matched_reference_ids'])-set(result['objects']['starting_reviewed']['matched_reference_ids'])
                        result['added_candidates'] = describe_additions(raster, merged & ~masks['starting'], reference,
                                                                       parts, params, new_ids)
                if fresh is None and result['objects']['starting_reviewed'] != old_ranker[uri]:
                    raise ValueError('起点模型加复核未复现已保存结果')
                rows.append(result)
                if index % 16 == 0:
                    resource.sample()
        usage = resource.sample()
    lanes = sorted({row['lane'] for row in rows})
    groups = {'all': rows, **{lane: [row for row in rows if row['lane'] == lane] for lane in lanes}}
    results = {lane: {name: tally(items, name) for name in rows[0]['objects']} for lane, items in groups.items()}
    output.mkdir(parents=True)
    atomic_json(output/'images.json', rows)
    report = {'state': 'completed', 'created_utc': now(), 'results': results, 'resource': usage,
              'added_candidate_diagnostics': dict(Counter(item['classification'] for row in rows for item in row.get('added_candidates', []))),
              'scope': 'Development replay, thresholds from the frozen original candidate ranker. No score refitting or release.',
              'supplement_threshold': supplement_threshold,
              'supplement_scope': 'Exploratory follow-up; independent candidate below 512 px; training negative upper tail only.' if supplement else None,
              'fresh_cohort_sha256': fresh_sha, 'frozen_supplement_sha256': threshold_evidence,
              'segmentation_summary_sha256': digest(study/'summary.json'), 'ranker_summary_sha256': digest(ranker/'summary.json'),
              'images_sha256': digest(output/'images.json'), 'code_sha256': digest(__file__),
              'test_accessed': False, 'calibration_accessed': False, 'deployed': False}
    atomic_json(output/'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('study', 'source', 'cohort', 'ranker', 'output'):
        parser.add_argument('--'+key, required=True)
    parser.add_argument('--supplement', action='store_true')
    parser.add_argument('--fresh')
    parser.add_argument('--frozen-supplement')
    configure_environment()
    report = run(**vars(parser.parse_args()))
    print(json.dumps(report['results']['all']))


if __name__ == '__main__':
    main()
