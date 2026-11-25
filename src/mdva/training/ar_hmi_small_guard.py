"""Research replay of an area guard for the fitted physical candidate verifier."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import label

from .ar_hmi_candidate_ranker import LANES, tally
from .ar_region_retention import filter_by_area
from .ar_scale_review import native_objects
from .datasets import SegmentationDataset
from .durable import atomic_json
from .forecast_region_detector_audit import MODEL
from .journal import read
from .runtime import digest, now, output_path


PROTECTED_AREA = 512


def keep_candidate(area, score, threshold):
    # A candidate at least twice as large as a <256 px reference cannot match
    # it at IoU >= 0.5. Preserve every candidate below that bound.
    return area < PROTECTED_AREA or score is None or score >= threshold


def run(dataset, diagnostic, previous, output):
    dataset, diagnostic, previous = (Path(p).resolve() for p in (dataset, diagnostic, previous))
    output = output_path(output, dataset)
    if output.exists() or any(output.is_relative_to(path) for path in (diagnostic, previous)):
        raise ValueError('小区域保护对照使用新目录')
    summary = read(previous / 'summary.json')
    if (summary['state'] != 'completed' or summary['test_accessed'] or summary['calibration_accessed']
            or summary['images_sha256'] != digest(previous / 'images.json')
            or summary['rankers_sha256'] != digest(previous / 'rankers.json')
            or read(diagnostic / 'identity.json')['test_accessed']):
        raise ValueError('需已完成的开发集物理复核')
    index = {r['uri']: r for r in read(diagnostic / 'case-index.json')
             if r['run_id'] == MODEL and r['diagnostic_subset'] == 'validation'}
    validation = SegmentationDataset(dataset, 'AR', 'validation')
    data = {r['uri']: r for r in validation.rows}
    rows = read(previous / 'images.json')
    threshold = read(previous / 'rankers.json')['physical']['review_threshold']
    for row in rows:
        uri = row['uri']
        frozen = data[uri]
        item = index[uri]
        score_path = diagnostic / 'probabilities' / MODEL / (item['sample_id'] + '.npy')
        if item['mask_uri'] != frozen['mask_uri'] or digest(score_path) != item['probability_sha256']:
            raise ValueError('原概率和参考身份不同')
        reference = np.load(validation.source(frozen['mask_uri']), allow_pickle=False)
        probability = np.load(score_path, allow_pickle=False)
        if probability.shape != reference.shape or not np.isfinite(probability).all():
            raise ValueError('概率数组不同网格或非有限')
        mask = filter_by_area(probability >= .4, 64)
        if native_objects(mask, reference) != row['objects']['baseline']:
            raise ValueError('原候选不能复现')
        raster, count = label(mask)
        areas = np.bincount(raster.ravel(), minlength=count+1)
        saved = row['candidates']
        if count != len(saved) or any(r['component'] != k or r['area'] != areas[k] for k, r in enumerate(saved, 1)):
            raise ValueError('候选编号与原复核不一致')
        keeping = [False, *(keep_candidate(r['area'], r['scores']['physical'], threshold) for r in saved)]
        row['objects']['physical_with_small_guard'] = native_objects(np.asarray(keeping)[raster], reference)
    results = {lane: {name: tally([r for r in rows if r['lane'] == lane], name)
                      for name in ('baseline', 'physical', 'physical_with_small_guard')} for lane in LANES}
    output.mkdir(parents=True)
    atomic_json(output / 'images.json', rows)
    result = {'state': 'completed', 'created_utc': now(), 'area_guard_lt': PROTECTED_AREA, 'results': results,
              'scope': 'Developed after the observed tiny miss; same-cohort exploratory replay, requiring an independent confirmation.',
              'previous_sha256': digest(previous / 'summary.json'), 'images_sha256': digest(output / 'images.json'),
              'code_sha256': digest(__file__), 'test_accessed': False, 'calibration_accessed': False, 'deployed': False}
    atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('dataset', 'diagnostic', 'previous', 'output'):
        parser.add_argument('--' + key, required=True)
    result = run(**vars(parser.parse_args()))
    print(json.dumps(result['results']))


if __name__ == '__main__':
    main()
