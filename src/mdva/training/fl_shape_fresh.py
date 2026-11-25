"""Verify the frozen H-alpha contour candidate on independently selected archive dates."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import re

import cv2
from scipy.ndimage import label
import torch

from .durable import atomic_json
from .fl_direct_centers import architecture, infer, input_planes
from .fl_direct_reviewer import evidence
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT
from .fl_magfilo_audit import ARCHIVE_URL, DATA_SHA256, raster
from .fl_sparse_reviewer import download
from .fl_visual_candidate_review import measure
from .journal import read
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


PRIOR_FILES = {'protocol.json', 'summary.json', 'selection.json', 'receipt.json',
               'image-label-identity.json', 'download.json'}
NAME = re.compile(r'(20(?:18|19|20|21|22)[01][0-9][0-3][0-9])[0-9]{6}[A-Z]h')


def observed_days(paths):
    used, files = set(), {}
    for path in sorted(set(paths)):
        used_here = set()
        for text in NAME.findall(path.read_text(encoding='utf-8-sig')):
            try:
                day = datetime.strptime(text, '%Y%m%d').date()
            except ValueError:
                continue
            used_here.add(day)
        if used_here:
            used.update(day + timedelta(days=offset) for day in used_here for offset in (-1, 0, 1))
            files[path.relative_to(ROOT).as_posix()] = digest(path)
    return used, files


def incumbent_evidence(study, source):
    roots = [study, study.with_name('recognition-improvement-20261117'), study.with_name('recognition-improvement-20261116'),
             study.with_name('solar-observation-audit-20261118'), source]
    for folder in roots:
        for path in folder.rglob('*.json'):
            if path.parent.name.startswith('fl-shape-fresh-'):
                continue
            if folder.name.startswith('recognition-') and not path.parent.name.startswith('fl-'):
                continue
            if (path.name in PRIOR_FILES or 'cases' in path.name) and path.stat().st_size < 20_000_000:
                yield path


def select(document, excluded):
    labels = defaultdict(list)
    for row in document['annotations']:
        labels[row['image_id']].append(row)
    images = {}
    seen = set()
    for row in document['images']:
        observed = datetime.fromisoformat(row['date_captured'])
        if not 2018 <= observed.year <= 2022 or observed.date() in excluded:
            continue
        name, identity = row['file_name'], row['id']
        link = ARCHIVE_URL.fullmatch(row['url'])
        stamp = observed.strftime('%Y%m%d%H%M%S')
        if (not link or link['name']+'.jpeg' != name or link['day'] != stamp[:8]
                or link['month'] != stamp[:6] or link['name'][:14] != stamp
                or (row['width'], row['height']) != (2048, 2048) or identity in seen):
            raise ValueError('档案文件时刻、坐标或标注身份有冲突')
        seen.add(identity)
        entry = images.setdefault(name, {'file':name, 'source':row['url'], 'site':name[-7:-5],
                                         'observed_utc':observed.isoformat(), 'variants':[]})
        if entry['source'] != row['url'] or entry['observed_utc'] != observed.isoformat():
            raise ValueError('同一档案观测存在多重身份')
        entry['variants'].append(identity)
    return [images[name] for name in sorted(images)], labels


def verified_checkpoint(path, sha, expected):
    if digest(path) != sha:
        raise ValueError('冻结模型权重不符')
    state = torch.load(path, map_location='cpu', weights_only=True)
    if state.get('identity', state.get('protocol_sha256')) != expected:
        raise ValueError('冻结模型内部身份不符')
    net = architecture()
    net.load_state_dict(state['model'])
    return net


def run(source, study, output):
    source, study, output = source.resolve(), study.resolve(), output_path(output, DATASET)
    if not output.name.startswith('fl-shape-fresh-'):
        raise ValueError('使用独立的暗条新档案结果目录')
    candidate, control = study/'fl-shape-objective-01', study/'fl-direct-centers-01'
    reports = read(candidate/'summary.json'), read(control/'summary.json')
    archive = source/'magfilo_2024_v1.0.json'
    if (digest(archive) != DATA_SHA256 or any(r['test_accessed'] for r in reports)
            or any(digest(p/'protocol.json') != r['protocol_sha256'] for p,r in zip((candidate,control),reports,strict=True))):
        raise ValueError('源标注或冻结研究身份不符')
    arms = {
        'control': verified_checkpoint(control/'direct.pt', reports[1]['model_sha256'], reports[1]['protocol_sha256']),
        'edge_centerline': verified_checkpoint(candidate/'edge_centerline/last.pt', reports[0]['checkpoints']['edge_centerline'],
                                             [reports[0]['protocol_sha256'], 'edge_centerline'])}
    earlier, previous_inputs = observed_days(incumbent_evidence(study,source))
    chosen, labels = select(read(archive), earlier)
    if not chosen:
        raise ValueError('没有符合日期隔离的可用档案')
    protocol = {'code_sha256':digest(__file__), 'archive_sha256':DATA_SHA256, 'historic_inputs':previous_inputs,
        'candidate_summary_sha256':digest(candidate/'summary.json'), 'control_summary_sha256':digest(control/'summary.json'),
        'selection':'all eligible 2018–2022 originals at least two UTC dates from previously inventoried research observations',
        'arms':['control','edge_centerline','union_diagnostic'], 'threshold':.5, 'samples':chosen, 'test_accessed':False}
    saved = read(output/'protocol.json')
    if saved is None:
        atomic_json(output/'protocol.json', protocol)
    elif saved != protocol:
        raise ValueError('新档案样本或历史凭据已改变')
    existing = read(output/'summary.json')
    if existing and existing['state'] == 'completed':
        raise ValueError('该冻结研究已经完成')
    known = {r['file']:r for r in read(output/'download.json', []) if 'sha256' in r}
    pending = [row for row in chosen if row['file'] not in known]
    with ThreadPoolExecutor(max_workers=6) as pool:
        known.update((row['file'],row) for row in pool.map(lambda x: download(output,x),pending))
    receipts = [known[row['file']] for row in chosen]
    atomic_json(output/'download.json', receipts)
    with_bytes = [r for r in receipts if 'sha256' in r]
    if len({r['sha256'] for r in with_bytes}) != len(with_bytes):
        raise ValueError('检测到字节完全相同的不同观测')
    bad = [r['file'] for r in receipts if 'error' in r]
    if bad:
        atomic_json(output/'status.json', {'state':'incomplete_archive', 'unavailable':bad})
        return
    cv2.setNumThreads(2)
    torch.set_num_threads(2)
    rows = []
    with exclusive(ROOT/'.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.backends.cuda.matmul.allow_tf32=False
        torch.backends.cudnn.allow_tf32=False
        for net in arms.values():
            net.cuda().eval()
        meter = Resources()
        for receipt in receipts:
            path=output/'jpeg'/receipt['file']
            if digest(path)!=receipt['sha256']:
                raise ValueError('独立档案图像指纹不符')
            inputs, valid, _, _ = input_planes(path.read_bytes())
            references = [label(raster(labels[i]), structure=CONNECTIVITY)[0] for i in receipt['variants']]
            masks = {name:infer(net,inputs,valid)[0]>=.5 for name,net in arms.items()}
            masks['union_diagnostic'] = masks['control'] | masks['edge_centerline']
            frame = {arm: {'base_count':0, 'count':label(mask,structure=CONNECTIVITY)[1],
                'geometry':evidence(mask,references)} for arm,mask in masks.items()}
            rows.append({'file':receipt['file'], 'site':receipt['site'], 'arms':frame})
            meter.sample()
        resources=meter.sample()
    results = {arm: measure([row['arms'][arm] for row in rows],'all',0.) for arm in protocol['arms']}
    blank = {}
    for arm in protocol['arms']:
        frames = [row['arms'][arm] for row in rows]
        items = [(f, g) for f in frames for g in f['geometry'] if not g['reference_ids']]
        blank[arm] = {'versions':len(items), 'versions_with_candidates':sum(f['count']>0 for f,_ in items)}
    atomic_json(output/'new_archive-cases.json',rows)
    atomic_json(output/'summary.json', {'state':'completed','recorded_utc':now(),
        'protocol_sha256':digest(output/'protocol.json'),'download_sha256':digest(output/'download.json'),
        'results':{'new_archive':results}, 'blank_reference_observation':blank,
        'sites':dict(Counter(r['site'] for r in rows)),'images':len(rows),
        'test_accessed':False,'deployed':False,'resources':resources})
    atomic_json(output/'status.json', {'state':'completed','summary_sha256':digest(output/'summary.json')})


if __name__ == '__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source','study','output'):
        parser.add_argument('--'+name,required=True,type=Path)
    run(**vars(parser.parse_args()))
