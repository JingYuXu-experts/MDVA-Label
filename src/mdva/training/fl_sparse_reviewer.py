"""Compare frame-relative dark-filament review with the frozen global reviewer."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path
import re
import subprocess

import cv2
import numpy as np
from scipy.stats import rankdata
from sklearn.ensemble import HistGradientBoostingClassifier
from threadpoolctl import threadpool_limits
import torch

from .durable import atomic_json, commit
from .fl_author_model import AuthorGraph
from .fl_contour_families import cutoff, make_model
from .fl_direct_centers import architecture
from .fl_direct_reviewer import material
from .fl_ensemble_study import DATASET, ROOT
from .fl_magfilo_audit import ARCHIVE_URL, DATA_SHA256, group_observations
from .fl_magfilo_transfer import load_gray
from .fl_visual_candidate_review import measure
from .journal import read
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


CONTEXT_COLUMNS = (0, 1, 11)  # mean contour score, peak center score, local dark contrast
ARMS = ('global', 'balanced', 'relative')
MIN_RETAINED = .98


def frame_features(values, relative):
    values = np.asarray(values, dtype=float).reshape(-1, 15)
    if not relative or not len(values):
        return values
    signals = values[:, CONTEXT_COLUMNS]
    ranks = (rankdata(signals, axis=0) - .5) / len(values)
    return np.column_stack((values, ranks, signals - np.median(signals, axis=0)))


def fit_balanced(rows, relative):
    data = []
    for row in rows:
        targets = np.asarray(row['targets'])
        if not any(row['reference_counts']):
            # An all-empty reference supplies no independently confirmed background.
            continue
        values = frame_features(row['features'], relative)
        weight = 1. if row['variants'] >= 2 else .6
        for target in (0, 1):
            selected = targets == target
            count = int(selected.sum())
            if count:
                # Temper the contribution from images producing many candidates of one class.
                data.extend((feature, target, weight / np.sqrt(count)) for feature in values[selected])
    x, y, w = map(np.asarray, zip(*data, strict=True))
    mass = np.bincount(y, weights=w, minlength=2)
    if min(mass) <= 0:
        raise ValueError('复核需要两类有效样本')
    weights = w / mass[y] * len(w) / 2
    model = HistGradientBoostingClassifier(max_iter=90, max_leaf_nodes=12, l2_regularization=2.,
                                          learning_rate=.065, early_stopping=False, random_state=2026)
    model.fit(x, y, sample_weight=weights)
    return model, np.bincount(y, minlength=2).tolist()


def parsed_day(name):
    return date(int(name[:4]), int(name[4:6]), int(name[6:8]))


def excluded_days(names):
    return {parsed_day(name) + timedelta(days=offset) for name in names for offset in (-1, 0, 1)}


def download(folder, record):
    url = record['source']
    if not ARCHIVE_URL.fullmatch(url):
        raise ValueError('GONG 档案地址未通过校验')
    for _ in range(2):
        try:
            response = subprocess.run(['curl.exe', '--fail', '--silent', '--show-error',
                '--connect-timeout', '8', '--max-time', '80', '--max-filesize', str(4 * 1024 * 1024),
                url], capture_output=True, timeout=85, check=False)
            if response.returncode:
                failure = f'curl_{response.returncode}'
                continue
            raw = response.stdout
            if not 0 < len(raw) <= 4 * 1024 * 1024:
                raise ValueError('GONG 档案图像尺寸超限')
            load_gray(raw)
            commit(folder / 'jpeg' / record['file'], lambda stream: stream.write(raw))
            return {**record, 'sha256': sha256(raw).hexdigest()}
        except (subprocess.TimeoutExpired, OSError, ValueError) as error:
            failure = type(error).__name__
    return {**record, 'error': failure}


def with_counts(row):
    return {**row, 'reference_counts': [len(v['reference_ids']) for v in row['standalone']['geometry']]}


def archive_records(source_file, prior, control, detector):
    groups, refs = group_observations(read(source_file))
    used_fit = control['samples']['fit'] + detector['samples']['fit']
    histories = [path for path in prior.glob('fl-*/protocol.json')
                 if not path.parent.name.startswith('fl-sparse-reviewer-')]
    previous_days = excluded_days([name for path in histories
                                   for name in re.findall(r'2018[0-9]{10}[A-Z]h',
                                                          path.read_text(encoding='utf-8'))])
    blocked = excluded_days(used_fit)
    fitting, verification = [], []
    for url, variants in sorted(groups.items()):
        name = ARCHIVE_URL.fullmatch(url)['name'] + '.jpeg'
        day = parsed_day(name)
        ids = [item['id'] for item in variants]
        record = {'file': name, 'source': url, 'variants': ids,
                  'annotation_counts': [len(refs[item]) for item in ids]}
        if day.year <= 2016 and day not in blocked and 0 < max(record['annotation_counts']) <= 2:
            fitting.append(record)
        if day.year == 2018 and day not in previous_days:
            verification.append(record)
    return fitting, verification, refs


def rank_cases(rows, models):
    cases = []
    for row in rows:
        frame = {**row['standalone'], 'scores': {}}
        for arm, model in models.items():
            x = frame_features(row['features'], arm == 'relative')
            frame['scores'][arm] = model.predict_proba(x)[:, 1].tolist() if len(x) else []
        cases.append({'file': row['file'], 'frame': frame})
    return cases


def summarize(cases, thresholds):
    frames = [row['frame'] for row in cases]
    results = {'raw': measure(frames, 'all', 0.)}
    for arm, threshold in thresholds.items():
        kept = measure(frames, arm, threshold)
        empty = [(frame, geometry) for frame in frames for geometry in frame['geometry']
                 if not geometry['reference_ids']]
        kept['empty_reference_versions'] = len(empty)
        kept['nonempty_output_on_empty_reference_versions'] = sum(
            any(score >= threshold for score in frame['scores'][arm]) for frame, _ in empty)
        results[arm] = kept
    return results


def run(source, study, output):
    output = output_path(output, DATASET)
    if output.exists() or digest(source / 'magfilo_2024_v1.0.json') != DATA_SHA256:
        raise ValueError('需要新输出目录和冻结的 MAGFiLO 原始数据')
    control = study / 'fl-direct-reviewer-01'
    direct = study / 'fl-direct-centers-01'
    anchor = study / 'fl-magfilo-adaptation-01'
    sparse = source / 'archive-fresh-2019-2020-01'
    info = read(control / 'summary.json')
    protocol = read(control / 'protocol.json')
    detector = read(direct / 'protocol.json')
    direct_summary = read(direct / 'summary.json')
    weight_sha = digest(direct / 'direct.pt')
    if (info['test_accessed'] or direct_summary['test_accessed']
            or info['protocol_sha256'] != digest(control / 'protocol.json')
            or direct_summary['protocol_sha256'] != digest(direct / 'protocol.json')
            or weight_sha != protocol['model_sha256']
            or weight_sha != direct_summary['model_sha256']
            or digest(anchor / 'mixed_gong/last.pt') != read(anchor / 'summary.json')['results']['mixed_gong']['weights_sha256']):
        raise ValueError('候选生成与原复核身份不符')

    fit_extra, verify, refs = archive_records(source / 'magfilo_2024_v1.0.json', study, protocol, detector)
    sparse_identity = read(sparse / 'image-label-identity.json')
    if sparse_identity['receipt_sha256'] != digest(sparse / 'summary.json'):
        raise ValueError('稀疏日期来源清单指纹不符')
    sparse_records = sparse_identity['files']
    # Preserve all variants and verify each one against the original archived observation.
    original = read(source / 'magfilo_2024_v1.0.json')
    source_index = {row['id']: row for row in original['images']}
    annotated = defaultdict(list)
    for annotation in original['annotations']:
        annotated[annotation['image_id']].append(annotation)
    for record in sparse_records:
        if any(source_index[i]['file_name'] != record['file'] or
               source_index[i]['date_captured'] != record['time'] for i in record['variants']):
            raise ValueError('稀疏日期标注与原图不符')
        for ident in record['variants']:
            refs[ident] = annotated[ident]
    files = {name: digest(control / name) for name in ('fit-geometry.json', 'development-geometry.json',
                                                      '2019_2020-geometry.json')}
    atomic_json(output / 'protocol.json', {'created_utc': now(), 'recorded_inputs': files,
        'code_sha256': digest(__file__), 'control_sha256': digest(control / 'summary.json'),
        'archive_sha256': DATA_SHA256, 'archive_fit': fit_extra, 'new_2018': verify,
        'sparse_identity_sha256': digest(sparse / 'image-label-identity.json'),
        'selection': '2017 precision maximized with at least 98% of frozen reviewer matches',
        'matching': 'unchanged level 0.5, one-to-one IoU >= 0.5',
        'test_accessed': False, 'deployed': False})

    with ThreadPoolExecutor(max_workers=6) as pool:
        downloaded = list(pool.map(lambda row: download(output, row), [*fit_extra, *verify]))
    atomic_json(output / 'download.json', downloaded)
    available = {row['file']: row for row in downloaded if 'sha256' in row}
    state = torch.load(direct / 'direct.pt', map_location='cpu', weights_only=True)
    if state['protocol_sha256'] != direct_summary['protocol_sha256']:
        raise ValueError('直接检测检查点内协议不符')
    base, net = AuthorGraph('batch'), architecture()
    base.load_state_dict(load_checkpoint(anchor / 'mixed_gong/last.pt')['model'])
    net.load_state_dict(state['model'])
    cv2.setNumThreads(2)
    torch.set_num_threads(2)
    extra_rows = {}
    cohorts = {'fit': fit_extra, 'new_2018': verify, 'sparse_replay': sparse_records}
    with exclusive(ROOT / '.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        base.cuda().eval()
        net.cuda().eval()
        meter = Resources()
        for phase, records in cohorts.items():
            rows = []
            for record in records:
                name = record['file']
                if phase == 'sparse_replay':
                    raw = (sparse / 'jpeg' / name).read_bytes()
                    expected = record['jpeg_sha256']
                elif name in available:
                    raw = (output / 'jpeg' / name).read_bytes()
                    expected = available[name]['sha256']
                else:
                    continue
                if sha256(raw).hexdigest() != expected:
                    raise ValueError('Hα图像指纹不符')
                rows.append(with_counts({'file': name, **material(raw, [refs[i] for i in record['variants']], base, net)}))
                meter.sample()
            extra_rows[phase] = rows
            atomic_json(output / f'{phase}-geometry.json', rows)
        resources = meter.sample()

    known_fit = [with_counts(row) for row in read(control / 'fit-geometry.json')]
    combined = known_fit + extra_rows['fit']
    with threadpool_limits(limits=2):
        baseline, known_counts = make_model(known_fit, 15)
        balanced, counts = fit_balanced(combined, False)
        relative, _ = fit_balanced(combined, True)
    models = dict(zip(ARMS, (baseline, balanced, relative), strict=True))
    cohorts = {k: read(control / f'{k}-geometry.json') for k in ('development', '2019_2020')}
    cohorts.update({k: v for k, v in extra_rows.items() if k != 'fit'})
    threshold = {'global': info['policy']['standalone']['learned']}
    scores = {}
    for phase, rows in cohorts.items():
        cases = rank_cases(rows, models)
        if phase == 'development':
            frame = [row['frame'] for row in cases]
            minimum = int(np.ceil(info['scores'][phase]['retention']['learned']['matched'] * MIN_RETAINED))
            for arm in ARMS[1:]:
                threshold[arm] = cutoff([{**r, 'scores': {'review': r['scores'][arm]}} for r in frame], minimum)
            threshold = {key: value for key, value in threshold.items() if value is not None}
            atomic_json(output / 'thresholds.json', threshold)
        scores[phase] = {'images': len(rows), 'scores': summarize(cases, threshold) if rows else None}
        if phase in info['scores']:
            current = scores[phase]['scores']['global']
            known = info['scores'][phase]['retention']['learned']
            if any(current[k] != known[k] for k in ('matched', 'reference', 'predicted')):
                raise ValueError(f'{phase} 原复核结果未复现')
        atomic_json(output / f'{phase}-cases.json', cases)
    failures = Counter(row['error'] for row in downloaded if 'error' in row)
    result = {'state': 'partial' if failures else 'completed', 'recorded_utc': now(),
              'scores': scores, 'thresholds': threshold,
              'fit_images': len(combined), 'archive_fit_images': len(extra_rows['fit']),
              'fit_class_counts': {'original': known_counts, 'balanced': counts},
              'download_failures': dict(failures),
              'protocol_sha256': digest(output / 'protocol.json'), 'resources': resources,
              'test_accessed': False, 'deployed': False}
    atomic_json(output / 'summary.json', result)
    return result


if __name__ == '__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'study', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    print(run(**vars(parser.parse_args()))['state'])
