"""Check the frozen fused and whole-image reviewers on unseen 2019–2020 GONG data."""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path

import torch

from .durable import atomic_json
from .fl_author_model import AuthorGraph
from .fl_candidate_review import aggregate, apply_review, collect
from .fl_ensemble_study import DATASET, ROOT
from .fl_gong_fused_review import measure
from .fl_magfilo_audit import DATA_SHA256, raster
from .fl_magfilo_transfer import LABEL_SHA256, RELEASE_URL, fetch_image, load_gray, release_observations, same_annotation
from .journal import read
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


YEARS = ('2019', '2020')


def later_pairs(source, release):
    snapshots = read(source), read(release)
    originals, published = map(release_observations, snapshots)
    names = sorted(name for name in published if name[:4] in YEARS)
    if len(names) != 22:
        raise ValueError('冻结公开版本的年份清单不符')
    ann_sets = []
    for source in snapshots:
        by_image = defaultdict(dict)
        for row in source['annotations']:
            by_image[row['image_id']][row['id']] = row
        ann_sets.append(by_image)
    before, after = ann_sets
    records = []
    for name in names:
        authority = {r['id']: r for r in originals.get(name, [])}
        trusted = []
        for row in published[name]:
            key = row['id']
            if (key in authority and before[key].keys() == after[key].keys()
                    and all(same_annotation(value, after[key][aid]) for aid, value in before[key].items())):
                trusted.append(authority[key])
        if trusted:
            records.append({
                'file': name, 'captured_utc': trusted[0]['date_captured'],
                'annotators': trusted, 'role': 'external_2019_2020_verification',
            })
    return records, before


def run(source, release, adapted, patches, prior, fused, output):
    torch.set_num_threads(2)
    output = output_path(output, DATASET)
    if ((output / 'summary.json').exists() or any(output.is_relative_to(p.resolve()) for p in (adapted, patches, prior, fused))
            or digest(source) != DATA_SHA256 or digest(release) != LABEL_SHA256):
        raise ValueError('外部融合验证需要独立目录及固定公开来源')
    baseline, combined = (read(p / 'summary.json') for p in (prior, fused))
    prior_plan = read(prior / 'protocol.json')
    joined_plan = read(fused / 'protocol.json')
    inputs = adapted / 'mixed_gong' / 'last.pt', patches / 'detail_training' / 'last.pt'
    whole_sha, crop_sha = map(digest, inputs)
    prior_rules, fusion_rules = (p / 'rules.json' for p in (prior, fused))
    if (any(r['state'] != 'completed' or r['test_accessed'] or r['calibration_accessed'] for r in (baseline, combined))
            or not combined['passed'] or joined_plan['independent_accessed']
            or baseline['protocol_sha256'] != digest(prior / 'protocol.json')
            or combined['protocol_sha256'] != digest(fused / 'protocol.json')
            or baseline['rule_sha256'] != digest(prior_rules) or combined['rule_sha256'] != digest(fusion_rules)
            or prior_plan['weights_sha256']['gong'] != whole_sha
            or (combined['weights_sha256']['whole'], combined['weights_sha256']['crop']) != (whole_sha, crop_sha)):
        raise ValueError('全图与融合复核的冻结身份不符')
    rows, reference = later_pairs(source, release)
    used = {name for cohort in joined_plan['samples'].values() for name in cohort}
    if len(rows) < 18 or used & {row['file'] for row in rows}:
        raise ValueError('外部验证输入不足或与已有图像重合')
    plan = {
        'code_sha256': digest(__file__), 'image_release': RELEASE_URL, 'image_years': YEARS,
        'files': [r['file'] for r in rows], 'weights_sha256': [whole_sha, crop_sha],
        'ranker_sha256': [digest(prior_rules), digest(fusion_rules)],
        'criterion': 'Fused review strict recall +2pp, precision loss <=1pp versus reviewed whole',
        'test_accessed': False, 'calibration_accessed': False, '2021_accessed': False,
    }
    marker = output / 'protocol.json'
    plan['image_years'] = list(YEARS)
    if marker.is_file() and read(marker) != plan:
        raise ValueError('外部验证的规则变化')
    atomic_json(marker, plan)
    with ThreadPoolExecutor(6) as pool:
        observations = list(pool.map(lambda row: fetch_image(output, row), rows))
    atomic_json(output / 'inventory.json', observations)
    if any('image_sha256' not in r for r in observations):
        return {'state': 'incomplete', 'downloaded': sum('image_sha256' in r for r in observations)}
    if len({r['image_sha256'] for r in observations}) != len(observations):
        raise ValueError('外部验证图像内容重复')
    old_rule, new_rule = read(prior_rules)['gong'], read(fusion_rules)
    models = [AuthorGraph('batch') for _ in inputs]
    for model, path in zip(models, inputs, strict=True):
        model.load_state_dict(load_checkpoint(path)['model'])
    cases = {'whole': [], 'fused': []}
    with exclusive(ROOT / '.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        whole, crop = [model.cuda().eval() for model in models]
        usage = Resources()
        for row in observations:
            raw = (output / 'jpeg' / row['file']).read_bytes()
            if sha256(raw).hexdigest() != row['image_sha256']:
                raise ValueError('外部验证图像已变化')
            references = [raster(reference[a['id']].values()) for a in row['annotators']]
            anchor = collect(whole, load_gray(raw), references, row['file'], row['captured_utc'], row['file'][14:16])
            supplement = measure(whole, crop, raw, references, row)
            for group, scores, rule in (('whole', anchor, old_rule), ('fused', supplement, new_rule)):
                cases[group].append(apply_review(scores, rule['classifier'], rule['cutoffs']))
            usage.sample()
        performance = usage.sample()
    scores = {name: aggregate(rows) for name, rows in cases.items()}
    old, new = (scores[name]['shape_and_brightness'] for name in ('whole', 'fused'))
    gate = new['recall'] >= old['recall'] + .02 and new['precision'] >= old['precision'] - .01
    atomic_json(output / 'cases.json', cases)
    summary = {
        'state': 'completed', 'created_utc': now(), 'images': len(observations),
        'passes_incremental_gate': gate, 'scores': scores, 'protocol_sha256': digest(marker),
        'cases_sha256': digest(output / 'cases.json'), 'inventory_sha256': digest(output / 'inventory.json'),
        'test_accessed': False, 'calibration_accessed': False, '2021_accessed': False,
        'deployed': False, 'resources': performance,
    }
    atomic_json(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'release', 'adapted', 'patches', 'prior', 'fused', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    configure_environment()
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))


if __name__ == '__main__':
    main()
