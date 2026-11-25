"""Check whether filtering weak magnetic candidates on quiet dates loses labeled regions."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import numpy as np
from scipy.ndimage import label

from .ar_quiet_window_audit import FIELD_FLOOR, MIN_STRONG_PIXELS, physical_stats
from .datasets import SegmentationDataset, read_frozen
from .durable import atomic_json
from .journal import read
from .recognition_approximate_audit import pair_regions, totals
from .runtime import digest, now, output_path


RUN = 'AR-A0-s42'


def signed_support_filter(binary, components):
    labels, count = label(binary)
    if len(components)!=count or any(c['component']!=i for i,c in enumerate(components,1)):
        raise ValueError('物理统计与活动区连通域不一致')
    keep = np.array([False, *(c['both_signs_supported'] for c in components)],dtype=bool)
    return keep[labels]


def run(dataset,diagnostic,archives,output):
    output = output_path(output,dataset)
    if (output/'summary.json').exists():
        raise ValueError('使用独立的检查版本')
    identity = read(diagnostic/'identity.json')
    report = read(diagnostic/'report.json')
    if report['state']!='completed' or report['test_unlocked'] or identity['test_accessed']:
        raise ValueError('需要仅用开发集的已完成概率缓存')
    checkpoint = next(r['checkpoint_sha256'] for r in identity['checkpoints'] if r['id']==RUN)
    cached = {r['uri']:r for r in read(diagnostic/'case-index.json')
              if r['run_id']==RUN and r['diagnostic_subset']=='validation'}
    series = SegmentationDataset(dataset,'AR','validation')
    valid = {r['uri']:r for r in series.rows}
    files = {r['uri']:r['sha256'] for r in read_frozen(dataset,'source-files.jsonl')}

    available = {}
    for folder in archives:
        summary = read(folder/'summary.json')
        if (summary['state']!='completed' or summary['test_accessed'] or summary['calibration_accessed']
                or digest(folder/'protocol.json')!=summary['protocol_sha256']):
            raise ValueError('物理对照档案身份不符')
        for row in summary['results']:
            if row['split']!='validation' or row['state']!='display_alignment_supported':
                continue
            uri = row['uri']
            path = folder/'pairs'/str(int(row['id']))/'aligned-los.npy'
            if uri in available or uri not in valid or digest(path)!=row['array_sha256']:
                raise ValueError('物理对照重复、超出开发范围或哈希不符')
            available[uri] = (path,row['array_sha256'])
    if not available:
        raise ValueError('缺少通过配准的开发集对照')
    plan = {'state':'frozen','code_sha256':digest(__file__),'manifest_sha256':digest(dataset/'manifest-hashes.json'),
        'diagnostic_index_sha256':digest(diagnostic/'case-index.json'),'checkpoint_sha256':checkpoint,
        'archives':{str(path):digest(path/'summary.json') for path in archives},
        'field_proxy':{'minimum_abs_gauss':FIELD_FLOOR,'min_pixels_per_sign':MIN_STRONG_PIXELS},
        'meaning':'Illustrative signed field screen on current candidates, no parameter fitting or release',
        'split':'validation','test_accessed':False,'calibration_accessed':False}
    atomic_json(output/'protocol.json',plan)
    rows = []
    for uri,(path,expected) in sorted(available.items()):
        record = cached[uri]
        mask = valid[uri]['mask_uri']
        p_path = diagnostic/'probabilities'/RUN/(record['sample_id']+'.npy')
        mask_path = series.source(mask)
        if (record['split']!='validation' or record['checkpoint_sha256']!=checkpoint
                or record['mask_uri']!=mask or digest(p_path)!=record['probability_sha256']
                or digest(path)!=expected or digest(mask_path)!=files[mask]):
            raise ValueError('缓存、磁图或参考网格身份不符')
        probability = np.load(p_path,allow_pickle=False)
        field = np.load(path,allow_pickle=False)
        reference = np.load(mask_path,allow_pickle=False)
        if (probability.shape!=(1024,1024) or field.shape!=probability.shape
                or reference.shape!=probability.shape or not np.isfinite(probability).all()):
            raise ValueError('物理、候选和人工标注需使用同一有限概率网格')
        binary = probability>=.5
        objects = physical_stats(binary,field)
        kept = signed_support_filter(binary,objects)
        initial = pair_regions(binary,reference,'AR')
        filtered = pair_regions(kept,reference,'AR')
        ids = {r['reference'] for r in initial['matching_regions']}
        current = {r['reference'] for r in filtered['matching_regions']}
        rows.append({'uri':uri,'initial':initial,'screened':filtered,'lost_reference_ids':sorted(ids-current),
                     'gained_reference_ids':sorted(current-ids),'candidates_prior':objects})
    atomic_json(output/'images.json',rows)
    changes = Counter(lost=sum(len(r['lost_reference_ids']) for r in rows),
                      gained=sum(len(r['gained_reference_ids']) for r in rows))
    result = {key:totals([r[key] for r in rows]) for key in ('initial','screened')}
    atomic_json(output/'summary.json',{'state':'completed','recorded_utc':now(),
        'images':len(rows),'changes':dict(changes),'result':result,'images_sha256':digest(output/'images.json'),
        'protocol_sha256':digest(output/'protocol.json'),'test_accessed':False,'calibration_accessed':False})


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset','diagnostic','output'):
        parser.add_argument('--'+name,required=True,type=Path)
    parser.add_argument('--archives',nargs='+',required=True,type=Path)
    run(**vars(parser.parse_args()))
