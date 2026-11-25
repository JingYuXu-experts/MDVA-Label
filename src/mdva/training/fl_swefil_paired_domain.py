"""Replay matched SWEFil image variants with both region and instance scoring."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from scipy.ndimage import label
import torch

from .durable import atomic_json
from .fl_direct_centers import infer, input_planes
from .fl_direct_reviewer import evidence
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT
from .fl_magfilo_audit import raster
from .fl_magfilo_transfer import load_gray
from .fl_shape_fresh import verified_checkpoint
from .fl_shape_statistics import paired
from .fl_swefil_review import FILAMENTS, TRAIN_SHA, fetch
from .fl_visual_candidate_review import measure
from .journal import read
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


MODES = ('raw', 'processed')
MODELS = ('control', 'edge_centerline')


def variant_pairs(body, old, tree):
    index = {row['file_name']: row for row in body['images']}
    hashes = {row['path']: row['lfs']['oid'] for row in tree if row.get('lfs')}
    categories = {row['id']: row['name'] for row in body['categories']}
    labels = defaultdict(list)
    for row in body['annotations']:
        labels[row['image_id']].append(row)
    items = []
    for raw in old:
        file = raw['file'].removesuffix('_raw.jpg') + '.jpg'
        if raw['file'] not in index or raw['id'] != index[raw['file']]['id'] or hashes[raw['file']] != raw['sha256']:
            raise ValueError('原图与前序实验身份不符')
        other = index[file]
        original = [(a['category_id'], a['segmentation']) for a in labels[raw['id']]]
        processed = [(a['category_id'], a['segmentation']) for a in labels[other['id']]]
        if original != processed or (other['width'], other['height']) != (2048, 2048):
            raise ValueError('原图与处理图的标注坐标或类别不同')
        items.append({'raw':raw, 'processed':{'file':file,'id':other['id'],'sha256':hashes[file]},
            'filament_labels':[item for item in labels[raw['id']] if categories[item['category_id']] in FILAMENTS]})
    return items


def alignment(raw, processed):
    """Measure local texture displacement in five fixed on-disk patches."""
    base, alternate = (image - cv2.GaussianBlur(image, (0, 0), 5.)
                       for image in (load_gray(raw), load_gray(processed)))
    offsets = []
    for y, x in ((256,256),(171,171),(171,341),(341,171),(341,341)):
        part = base[y-24:y+24,x-24:x+24]
        search = alternate[y-36:y+36,x-36:x+36]
        scores = cv2.matchTemplate(search, part, cv2.TM_CCOEFF_NORMED)
        _, quality, _, (start_x, start_y) = cv2.minMaxLoc(scores)
        offsets.append({'dx':start_x-12,'dy':start_y-12,'correlation':quality})
    return offsets


def score_instances(predicted, reference_masks, ids):
    """Score each original annotation, including polygons that overlap or split."""
    instances, count = label(predicted, structure=CONNECTIVITY)
    areas = np.bincount(instances.ravel(), minlength=count+1)[1:]
    target_areas = []
    columns = []
    for mask in reference_masks:
        pixels = int(mask.sum())
        if not pixels:
            raise ValueError('作者实例无法在目标网格栅格化')
        hits = np.bincount(instances[mask], minlength=count+1)[1:]
        target_areas.append(pixels)
        columns.append(hits/(areas + pixels - hits))
    matrix = np.stack(columns, axis=1) if columns else np.empty((count,0))
    return {'base_count':0,'count':count,'geometry':[{'reference_ids':ids,
        'reference_sizes':target_areas,'iou':matrix.tolist()}]}


def run(source, study, output):
    output = output_path(output,DATASET)
    if (output/'summary.json').exists() or digest(source/'train.json') != TRAIN_SHA:
        raise ValueError('需要固定发布版本和新研究目录')
    prior = study/'fl-swefil-review-01'
    run_info = read(prior/'summary.json')
    protocol = read(prior/'protocol.json')
    if (run_info['test_accessed'] or run_info['author_test_accessed']
            or run_info['protocol_sha256'] != digest(prior/'protocol.json')
            or protocol['source_image_tree_sha256'] != digest(source/'image-tree.json')):
        raise ValueError('配对验证的来源指纹不符')
    pairs = variant_pairs(read(source/'train.json'),protocol['samples'],read(source/'image-tree.json'))
    original = study/'fl-direct-centers-01'
    candidate = study/'fl-shape-objective-01'
    bas = read(original/'summary.json')
    new = read(candidate/'summary.json')
    models = {'control':verified_checkpoint(original/'direct.pt',protocol['weights']['control'],bas['protocol_sha256']),
        'edge_centerline':verified_checkpoint(candidate/'edge_centerline/last.pt',protocol['weights']['edge_centerline'],
                                             [new['protocol_sha256'],'edge_centerline'])}
    descriptor = {'code_sha256':digest(__file__),'previous_sha256':digest(prior/'summary.json'),
        'pairs':[{'raw':p['raw'],'processed':p['processed']} for p in pairs],
        'threshold':.5,'test_accessed':False,'author_test_accessed':False}
    recorded = read(output/'protocol.json')
    if recorded is None:
        atomic_json(output/'protocol.json',descriptor)
    elif recorded != descriptor:
        raise ValueError('研究配对身份已改变')
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(lambda p:fetch(output,p['processed']),pairs))
    atomic_json(output/'download.json',receipts)
    misses = [r['file'] for r in receipts if 'error' in r]
    if misses:
        atomic_json(output/'status.json',{'state':'incomplete_archive','unavailable':misses})
        return
    cv2.setNumThreads(2)
    torch.set_num_threads(2)
    results = []
    with exclusive(ROOT/'.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.backends.cuda.matmul.allow_tf32=False
        torch.backends.cudnn.allow_tf32=False
        for model in models.values():
            model.cuda().eval()
        resources = Resources()
        for data in pairs:
            raw, processed = data['raw'],data['processed']
            image_paths = {'raw':prior/raw['file'],'processed':output/processed['file']}
            raw_bytes = {}
            for mode, path in image_paths.items():
                if digest(path) != data[mode]['sha256']:
                    raise ValueError('配对图像字节身份已变')
                raw_bytes[mode] = path.read_bytes()
            labels = data['filament_labels']
            reference_masks = [raster([r]) for r in labels]
            references = label(raster(labels),structure=CONNECTIVITY)[0]
            report = {'file':Path(raw['file']).name,'site':raw['site'],
                'registration':alignment(raw_bytes['raw'],raw_bytes['processed']),
                'fragmented_instances':sum(label(ref,structure=CONNECTIVITY)[1]>1 for ref in reference_masks),
                'arms':{}, 'input_rejection':{}}
            for mode, payload in raw_bytes.items():
                try:
                    planes, valid, _, _ = input_planes(payload)
                except ValueError as error:
                    report['input_rejection'][mode] = str(error)
                    masks = dict.fromkeys(models,references < 0)
                else:
                    masks = {name:infer(model,planes,valid)[0]>=.5 for name,model in models.items()}
                for name, mask in masks.items():
                    key = f'{mode}_{name}'
                    count = label(mask,structure=CONNECTIVITY)[1]
                    report['arms'][key+'_region'] = {'base_count':0,'count':count,'geometry':evidence(mask,[references])}
                    report['arms'][key+'_instance'] = score_instances(mask,reference_masks,[r['id'] for r in labels])
                resources.sample()
            results.append(report)
        metrics = resources.sample()
    keys = list(results[0]['arms'])
    overall = {key:measure([r['arms'][key] for r in results],'all',0.) for key in keys}
    for name in MODELS:
        if overall[f'raw_{name}_region'] != run_info['results']['swefil'][name]:
            raise ValueError('原图与上一轮对照无法复现')
    comparisons = {f'{name}_{scope}': paired(results,f'processed_{name}_{scope}',f'raw_{name}_{scope}')
                   for name in MODELS for scope in ('region','instance')}
    atomic_json(output/'cases.json',results)
    alignment_counts = Counter()
    for row in results:
        good = sum(abs(tile['dx'])<=1 and abs(tile['dy'])<=1 and tile['correlation']>=.7 for tile in row['registration'])
        alignment_counts['verified_patches'] += good
        alignment_counts['images_with_three_verified_patches'] += int(good>=3)
    atomic_json(output/'summary.json',{'state':'completed','images':len(results),'recorded_utc':now(),
        'overall':overall,'paired':comparisons,'alignment':dict(alignment_counts),
        'fragmented_instances':sum(r['fragmented_instances'] for r in results),
        'input_rejections':dict(Counter(mode for r in results for mode in r['input_rejection'])),
        'protocol_sha256':digest(output/'protocol.json'),'cases_sha256':digest(output/'cases.json'),
        'resources':metrics,'test_accessed':False,'author_test_accessed':False,'deployed':False})
    atomic_json(output/'status.json',{'state':'completed','summary_sha256':digest(output/'summary.json')})


if __name__=='__main__':
    configure_environment()
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','study','output'):
        parser.add_argument('--'+name,required=True,type=Path)
    run(**vars(parser.parse_args()))
