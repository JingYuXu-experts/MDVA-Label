"""Compare AR probability averaging and majority voting across three existing seeds."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from .ar_region_retention import filter_by_area
from .ar_scale_review import estimate, native_objects
from .datasets import SegmentationDataset
from .durable import atomic_json
from .forecast_region_detector_audit import MODEL
from .journal import read
from .metrics import binary_segmentation
from .runtime import configure_environment, digest, now, output_path


MODELS = (MODEL, 'AR-A0-s17', 'AR-A0-s2026')
PIXEL_THRESHOLD = .4
MINIMUM_NATIVE_AREA = 256
POLICIES = ('control', 'mean', 'majority')


def masks(probabilities):
    if probabilities.shape != (len(MODELS), 1024, 1024) or not np.isfinite(probabilities).all():
        raise ValueError('需要三个原生网格的有限概率图')
    strong = probabilities >= PIXEL_THRESHOLD
    return {name: filter_by_area(mask, MINIMUM_NATIVE_AREA) for name, mask in zip(
        POLICIES, (strong[0], probabilities.mean(axis=0) >= PIXEL_THRESHOLD, strong.sum(axis=0) >= 2), strict=True)}


def run(dataset, diagnostic, prior, output):
    dataset, diagnostic, prior = (Path(p).resolve() for p in (dataset, diagnostic, prior))
    output = output_path(output, dataset)
    if output.exists() or any(output.is_relative_to(path) for path in (diagnostic, prior)):
        raise ValueError('同图对照需要新目录')
    old_summary = read(prior / 'summary.json')
    index_path = diagnostic / 'case-index.json'
    index_hash = digest(index_path)
    if (old_summary['test_accessed'] or read(diagnostic / 'identity.json')['test_accessed']
            or old_summary['index_sha256'] != index_hash or old_summary['images_sha256'] != digest(prior / 'images.json')
            or old_summary['manifest_sha256'] != digest(dataset / 'manifest-hashes.json')):
        raise ValueError('原评价数据身份不一致')
    control = {row['uri']: row['options']['0.40/64'] for row in read(prior / 'images.json')}
    index = {model: {} for model in MODELS}
    for row in read(index_path):
        if row['run_id'] in index and row['diagnostic_subset'] == 'validation':
            index[row['run_id']][row['uri']] = row
    validation = SegmentationDataset(dataset, 'AR', 'validation')
    expected = {row['uri'] for row in validation.rows}
    if len(expected) != 276 or set(control) != expected or any(set(items) != expected for items in index.values()):
        raise ValueError('三个种子的开发图像不同')
    sources = {model: {row['checkpoint_sha256'] for row in items.values()} for model, items in index.items()}
    if any(len(hashes) != 1 for hashes in sources.values()):
        raise ValueError('种子对应的检查点不唯一')
    protocol = {'models': {model: next(iter(hashes)) for model, hashes in sources.items()}, 'policies': POLICIES,
                'scope': 'Fixed comparison of existing A0 seed outputs, no weight fitting or threshold search.',
                'threshold': PIXEL_THRESHOLD, 'minimum_native_area': MINIMUM_NATIVE_AREA,
                'index_sha256': index_hash, 'prior_summary_sha256': digest(prior / 'summary.json'),
                'code_sha256': digest(__file__), 'test_accessed': False, 'calibration_accessed': False}
    output.mkdir(parents=True)
    atomic_json(output / 'protocol.json', protocol)

    rows = []
    for source in validation.rows:
        uri = source['uri']
        reference = np.load(validation.source(source['mask_uri']), allow_pickle=False)
        probabilities = []
        for model, cases in index.items():
            case = cases[uri]
            path = diagnostic / 'probabilities' / model / (case['sample_id'] + '.npy')
            if case['mask_uri'] != source['mask_uri'] or case['probability_sha256'] != digest(path):
                raise ValueError(f'{model}的开发概率缓存身份不符')
            probabilities.append(np.load(path, allow_pickle=False))
        measured = {name: {'objects': native_objects(mask, reference),
                           'pixels': binary_segmentation(mask, reference > 0)}
                    for name, mask in masks(np.stack(probabilities)).items()}
        baseline = measured['control']['objects']
        actual = tuple(baseline[k] for k in ('reference', 'predicted', 'matched'))
        prior_result = tuple(control[uri][k] for k in ('references', 'predicted', 'matched'))
        if actual != prior_result:
            raise ValueError('未复现原单种子区域评价')
        rows.append({'uri': uri, 'time_utc': source['time_utc'], 'arms': measured})
    arms = {name: estimate(rows, name) for name in POLICIES}
    criterion = {name: value['region_precision'] >= .8 and value['region_recall'] >= .8
                 for name, value in arms.items()}
    atomic_json(output / 'images.json', rows)
    result = {'state': 'completed', 'created_utc': now(), 'images': len(rows), 'arms': arms,
              'meets_both_80_on_development': criterion, 'test_accessed': False, 'calibration_accessed': False,
              'deployed': False, 'deployment_latency_measured': False,
              'protocol_sha256': digest(output / 'protocol.json'), 'images_sha256': digest(output / 'images.json')}
    atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('dataset', 'diagnostic', 'prior', 'output'):
        parser.add_argument('--' + key, required=True)
    configure_environment()
    result = run(**vars(parser.parse_args()))
    fields = ('region_precision', 'region_recall', 'matched_regions', 'false_positive_regions', 'by_reference_size')
    print(json.dumps({name: {field: scores[field] for field in fields} for name, scores in result['arms'].items()}))


if __name__ == '__main__':
    main()
