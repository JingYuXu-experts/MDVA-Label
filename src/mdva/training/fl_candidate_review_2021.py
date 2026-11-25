"""Verify the frozen GONG review rule on later, previously unused observations."""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

from safetensors.torch import save
import torch

from .fl_author_model import AuthorGraph
from .fl_candidate_review import aggregate, apply_review, collect
from .fl_ensemble_study import DATASET, ROOT
from .fl_magfilo_audit import DATA_SHA256, raster
from .fl_magfilo_transfer import LABEL_SHA256, RELEASE_URL, fetch_image, load_gray, release_observations, same_annotation
from .durable import atomic_json, commit
from .journal import read
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


def publicly_paired(source, release):
    originals, labels = read(source), read(release)
    main = release_observations(originals)
    packaged = release_observations(labels)
    dated = [name for name in packaged if name.startswith('2021')]
    if len(dated) != 36:
        raise ValueError('2021 年公开版本清单发生改变')
    records = []
    original_ann, packaged_ann = defaultdict(dict), defaultdict(dict)
    for version, groups in ((originals, original_ann), (labels, packaged_ann)):
        for row in version['annotations']:
            variants = groups[row['image_id']]
            if row['id'] in variants:
                raise ValueError('外部验证存在重复标注编号')
            variants[row['id']] = row
    for name in sorted(dated):
        authentic = {row['id']: row for row in main.get(name, [])}
        kept = []
        for row in packaged[name]:
            identity = row['id']
            before, published = original_ann[identity], packaged_ann[identity]
            if (identity in authentic and before.keys() == published.keys()
                    and all(same_annotation(value, published[k]) for k, value in before.items())):
                kept.append(authentic[identity])
        if kept:
            records.append({
                'file': name, 'role': 'external_2021_verification', 'annotators': kept,
                'captured_utc': datetime.fromisoformat(kept[0]['date_captured']).isoformat(),
            })
    return records, original_ann


def run(source, release, adaptation, prior, output):
    output = output_path(output, DATASET)
    if ((output / 'summary.json').exists() or any(output.is_relative_to(p.resolve()) for p in (adaptation, prior))
            or digest(source) != DATA_SHA256 or digest(release) != LABEL_SHA256):
        raise ValueError('外部验证输出位置或公开来源身份不符')
    base, review = (read(path / 'summary.json') for path in (adaptation, prior))
    weights = adaptation / 'mixed_gong' / 'last.pt'
    rules_file = prior / 'rules.json'
    checkpoint_hash = digest(weights)
    if (any(r['state'] != 'completed' or r['test_accessed'] or r['calibration_accessed'] for r in (base, review))
            or not review['domain_gate_passed']['gong'] or digest(rules_file) != review['rule_sha256']
            or checkpoint_hash != base['results']['mixed_gong']['weights_sha256']
            or checkpoint_hash != read(prior / 'protocol.json')['weights_sha256']['gong']
            or review['protocol_sha256'] != digest(prior / 'protocol.json')):
        raise ValueError('预先冻结的 GONG 候选复核配置不符')
    rows, annotations = publicly_paired(source, release)
    if len(rows) < 28:
        raise ValueError('可核验的独立外部图像不足')
    protocol = {
        'source': RELEASE_URL, 'version': 1, 'license': 'CC BY-NC 4.0', 'weights_sha256': checkpoint_hash,
        'annotation_sha256': DATA_SHA256, 'release_sha256': LABEL_SHA256, 'rule_sha256': digest(rules_file),
        'code_sha256': digest(__file__), 'verification_year': 2021, 'observation_files': [r['file'] for r in rows],
        'threshold': .5, 'gate': 'precision gain >=5pp and strict recall loss <=1pp; independent research only',
        'formal_test_accessed': False, 'calibration_accessed': False,
    }
    marker = output / 'protocol.json'
    if marker.exists() and read(marker) != protocol:
        raise ValueError('外部验证协议改变，不能复用旧记录')
    atomic_json(marker, protocol)
    with ThreadPoolExecutor(6) as workers:
        received = list(workers.map(lambda row: fetch_image(output, row), rows))
    atomic_json(output / 'inventory.json', received)
    chosen = [r for r in received if 'image_sha256' in r]
    if len(chosen) != len(rows):
        return {'state': 'incomplete', 'verified': len(chosen), 'total': len(rows)}
    if len({r['image_sha256'] for r in chosen}) != len(chosen):
        raise ValueError('外部验证图像重复')
    rules = read(rules_file)['gong']
    saved = load_checkpoint(weights)['model']
    model = AuthorGraph('batch')
    model.load_state_dict(saved)
    with exclusive(ROOT / '.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cuda.matmul.allow_tf32 = False
        model.cuda().eval()
        use = Resources()
        results = []
        for row in chosen:
            raw = (output / 'jpeg' / row['file']).read_bytes()
            if sha256(raw).hexdigest() != row['image_sha256']:
                raise ValueError('外部验证图像身份改变')
            refs = [raster(annotations[mark['id']].values()) for mark in row['annotators']]
            result = collect(model, load_gray(raw), refs, row['file'], row['captured_utc'], row['file'][14:16])
            results.append(apply_review(result, rules['classifier'], rules['cutoffs']))
            use.sample()
        resources = use.sample()
    scored = aggregate(results)
    unfiltered = scored['original']
    filtered = scored['shape_and_brightness']
    gate = filtered['precision'] >= unfiltered['precision'] + .05 and filtered['recall'] >= unfiltered['recall'] - .01
    candidate = None
    if gate:
        candidate = 'research-gong-weights.safetensors'
        commit(output / candidate, lambda stream: stream.write(save({k: v.contiguous() for k, v in saved.items()})))
        atomic_json(output / 'research-review-rule.json', rules)
    atomic_json(output / 'external-cases.json', results)
    summary = {
        'state': 'completed', 'created_utc': now(), 'images': len(results),
        'references': sum(len(r['geometry']) for r in results), 'verified': gate, 'metrics': scored,
        'candidate': candidate, 'candidate_sha256': digest(output / candidate) if candidate else None,
        'by_station': {s: aggregate([r for r in results if r['station'] == s])
                       for s in sorted({r['station'] for r in results})},
        'test_accessed': False, 'calibration_accessed': False, 'deployed': False,
        'protocol_sha256': digest(marker), 'cases_sha256': digest(output / 'external-cases.json'),
        'resources': resources,
    }
    atomic_json(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'release', 'adaptation', 'prior', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    configure_environment()
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))


if __name__ == '__main__':
    main()
