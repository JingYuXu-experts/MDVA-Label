"""Evaluate two frozen H-alpha models on the raw domain of the SWEFil release."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
import re
import subprocess

import cv2
from scipy.ndimage import label
import torch

from .durable import atomic_json, commit
from .fl_direct_centers import infer, input_planes
from .fl_direct_reviewer import evidence
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT
from .fl_magfilo_audit import raster
from .fl_magfilo_transfer import load_gray
from .fl_shape_fresh import incumbent_evidence, observed_days, verified_checkpoint
from .fl_visual_candidate_review import measure
from .journal import read
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


REVISION = 'bc818c69a79266ad0fa91a539359485af57967cf'
TRAIN_SHA = '41856b9a30b336b102b2cf09173118c76e2a3ff53431315f306176f292f80ebc'
BASE = f'https://huggingface.co/datasets/antonio-reche/SWEFil/resolve/{REVISION}/'
RAW = re.compile(r'images/(?P<stamp>[0-9]{14})(?P<site>[BCLMTU]h)_raw\.jpg')
FILAMENTS = {'QRF', 'IRF', 'ARF'}
ARMS = ('control', 'edge_centerline', 'union_diagnostic')


def samples(body, tree, excluded):
    category = {row['id']: row['name'] for row in body['categories']}
    if set(category.values()) != {*FILAMENTS, 'SUNSPOT'}:
        raise ValueError('SWEFil类别声明已改变')
    kinds = defaultdict(Counter)
    labels = defaultdict(list)
    for item in body['annotations']:
        kind = category[item['category_id']]
        kinds[item['image_id']][kind] += 1
        if kind in FILAMENTS:
            labels[item['image_id']].append(item)
    links = {r['path']:r['lfs']['oid'] for r in tree if r.get('lfs')}
    found = {}
    for row in body['images']:
        match = RAW.fullmatch(row['file_name'])
        if not match:
            continue
        captured = datetime.strptime(match['stamp'], '%Y%m%d%H%M%S')
        if not 2018 <= captured.year <= 2022 or captured.date() in excluded:
            continue
        sha = links.get(row['file_name'], '')
        if (row['width'],row['height']) != (2048,2048) or not re.fullmatch('[0-9a-f]{64}',sha):
            raise ValueError('SWEFil图像尺寸或来源指纹不符')
        if row['file_name'] in found:
            raise ValueError('同一SWEFil图像有多个标注身份')
        found[row['file_name']] = {'file':row['file_name'],'id':row['id'],'site':match['site'],
            'sha256':sha,'captured_utc':captured.isoformat(),'label_counts':dict(kinds[row['id']])}
    return [found[name] for name in sorted(found)], labels


def fetch(output, row):
    path = output/row['file']
    if path.is_file() and digest(path) == row['sha256']:
        return row
    for _ in range(2):
        response = subprocess.run(['curl.exe','--fail','--location','--silent','--show-error',
            '--connect-timeout','10','--max-time','35','--max-filesize','4194304', BASE+row['file']],
            capture_output=True, check=False)
        if response.returncode == 0 and sha256(response.stdout).hexdigest() == row['sha256']:
            load_gray(response.stdout)
            commit(path,lambda stream:stream.write(response.stdout))
            return row
    return {**row,'error':'download_or_hash'}


def run(source, magfilo, study, output):
    source, magfilo, study, output = source.resolve(), magfilo.resolve(), study.resolve(), output_path(output,DATASET)
    tree = source/'image-tree.json'
    if (digest(source/'train.json') != TRAIN_SHA or read(source/'repository.json')['sha'] != REVISION
            or (output/'summary.json').exists()):
        raise ValueError('需要固定的SWEFil公开版本和独立研究目录')
    base, shape = study/'fl-direct-centers-01', study/'fl-shape-objective-01'
    orig, updated = read(base/'summary.json'), read(shape/'summary.json')
    if any(r['test_accessed'] or r['deployed'] for r in (orig,updated)):
        raise ValueError('暗条研究状态不符')
    nets = {
        'control':verified_checkpoint(base/'direct.pt',orig['model_sha256'],orig['protocol_sha256']),
        'edge_centerline':verified_checkpoint(shape/'edge_centerline/last.pt',updated['checkpoints']['edge_centerline'],
                                              [updated['protocol_sha256'],'edge_centerline'])}
    days, sources = observed_days(incumbent_evidence(study,magfilo))
    expanded = {d+timedelta(days=offset) for d in days for offset in range(-4,5)}
    selected, labels = samples(read(source/'train.json'),read(tree),expanded)
    if not selected:
        raise ValueError('未找到符合隔离条件的外部资料')
    protocol = {'code_sha256':digest(__file__),'revision':REVISION,'train_labels_sha256':TRAIN_SHA,
        'source_image_tree_sha256':digest(tree),'date_filter':'2018–2022 raw authors train files, ±5 days from prior GONG research',
        'prior_date_evidence':sources,'samples':selected,'threshold':.5,'arms':list(ARMS),
        'weights':{'control':orig['model_sha256'],'edge_centerline':updated['checkpoints']['edge_centerline']},
        'test_accessed':False,'author_test_accessed':False}
    recorded = read(output/'protocol.json')
    if recorded is None:
        atomic_json(output/'protocol.json',protocol)
    elif recorded != protocol:
        raise ValueError('外部研究日期范围或身份已改变')
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(lambda row:fetch(output,row),selected))
    atomic_json(output/'download.json',receipts)
    misses = [r['file'] for r in receipts if 'error' in r]
    if misses:
        atomic_json(output/'status.json',{'state':'incomplete_archive','unavailable':misses})
        return
    if len({r['sha256'] for r in receipts}) != len(receipts):
        raise ValueError('观测图像出现相同字节指纹')
    cv2.setNumThreads(2)
    torch.set_num_threads(2)
    rows = []
    with exclusive(ROOT/'.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.backends.cuda.matmul.allow_tf32=False
        torch.backends.cudnn.allow_tf32=False
        for net in nets.values():
            net.cuda().eval()
        meter = Resources()
        for row in receipts:
            raw = (output/row['file']).read_bytes()
            if sha256(raw).hexdigest()!=row['sha256']:
                raise ValueError('SWEFil推理字节身份不符')
            reference = label(raster(labels[row['id']]),structure=CONNECTIVITY)[0]
            rejection = None
            try:
                fields, valid, _, _ = input_planes(raw)
            except ValueError as error:
                rejection = str(error)
                masks = dict.fromkeys(nets, reference < 0)
            else:
                masks = {name:infer(net,fields,valid)[0]>=.5 for name,net in nets.items()}
            masks['union_diagnostic'] = masks['control'] | masks['edge_centerline']
            frame = {arm:{'base_count':0,'count':label(mask,structure=CONNECTIVITY)[1],
                          'geometry':evidence(mask,[reference])} for arm,mask in masks.items()}
            rows.append({'file':Path(row['file']).name,'site':row['site'],'rejection':rejection,'arms':frame})
            meter.sample()
        resources = meter.sample()
    atomic_json(output/'swefil-cases.json',rows)
    results = {name:measure([row['arms'][name] for row in rows],'all',0.) for name in ARMS}
    groups = defaultdict(list)
    for row in rows:
        groups[row['site']].append(row)
    by_site = {site:{arm:measure([r['arms'][arm] for r in cases],'all',0.) for arm in ARMS}
               for site,cases in groups.items()}
    atomic_json(output/'summary.json',{'state':'completed','recorded_utc':now(),
        'results':{'swefil':results},'sites':{site:len(group) for site,group in groups.items()},'by_site':by_site,
        'input_rejections':[row['file'] for row in rows if row['rejection']],
        'protocol_sha256':digest(output/'protocol.json'),'download_sha256':digest(output/'download.json'),
        'cases_sha256':digest(output/'swefil-cases.json'),'test_accessed':False,'author_test_accessed':False,
        'deployed':False,'resources':resources})
    atomic_json(output/'status.json',{'state':'completed','summary_sha256':digest(output/'summary.json')})


if __name__ == '__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source','magfilo','study','output'):
        parser.add_argument('--'+name,required=True,type=Path)
    run(**vars(parser.parse_args()))
