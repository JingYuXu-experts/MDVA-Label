"""Pin author-linked TFJS JSON/float arrays; never execute model pickle data."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

import numpy as np

from .durable import atomic_json,commit
from .runtime import digest,now,output_path

MODEL_BLOB='c8d2cf1514c183eac2469869beb19cf1547c36a3'
COMMIT='a73a848bbc7eb5ef012daa9373a83fd7ee8cfd9a'
PINNED_TREE_SHA256='72059aedb1ae28164d395b0ab9f66b9e24ab1863575b9ed05f828d69676fb026'


def tree_digest(rows):
    selected=[{k:r[k] for k in ('path','sha','size','type')} for r in rows]
    return hashlib.sha256(json.dumps(sorted(selected,key=lambda r:r['path']),sort_keys=True,separators=(',',':')).encode()).hexdigest()


def git_blob(raw):
    return hashlib.sha1(f'blob {len(raw)}\0'.encode('ascii')+raw).hexdigest()


def download(path,name,expected_git=None):
    from ..web.space_weather import download_cnn193_browser_asset
    target=path/name
    if target.exists():
        meta=json.loads(target.with_name(name+'.source.json').read_text(encoding='utf-8'))
        if digest(target)!=meta['sha256'] or meta['commit']!=COMMIT:
            raise ValueError('已有模型来源指纹改变，不覆盖')
        raw=target.read_bytes()
    else:
        raw,meta=download_cnn193_browser_asset(name)
        if expected_git and git_blob(raw)!=expected_git:raise ValueError('作者模型Git blob不符')
        commit(target,lambda stream:stream.write(raw))
        meta.update(sha256=hashlib.sha256(raw).hexdigest(),git_blob=git_blob(raw),bytes=len(raw),fetched_utc=now())
        atomic_json(target.with_name(name+'.source.json'),meta)
    if expected_git and git_blob(raw)!=expected_git:raise ValueError('缓存Git blob不符')
    return raw,meta


def snapshot(output,dataset,tree):
    output=output_path(output,dataset);output.mkdir(parents=True,exist_ok=True)
    tree=json.loads(Path(tree).read_text(encoding='utf-8'))
    known={p['path'].removeprefix('keras/'):p for p in tree if p['path'].startswith('keras/')}
    if tree_digest(list(known.values()))!=PINNED_TREE_SHA256:
        raise ValueError('目录清单与独立锁定的作者提交不符')
    raw,model_source=download(output,'model.json',MODEL_BLOB)
    model=json.loads(raw)
    names=[n for g in model['weightsManifest'] for n in g['paths']]
    if len(names)!=len(set(names)) or set(names)|{'model.json'}!=set(known):
        raise ValueError('模型清单与固定提交目录不一致')
    def fetch(name):
        raw,meta=download(output,name,known[name]['sha'])
        if len(raw)!=known[name]['size']:raise ValueError('模型分片大小不符')
        return meta
    with ThreadPoolExecutor(max_workers=2) as pool:sources=list(pool.map(fetch,names))
    tensors={}
    for group in model['weightsManifest']:
        raw=b''.join((output/name).read_bytes() for name in group['paths'])
        offset=0
        for weight in group['weights']:
            if weight['dtype']!='float32' or weight['name'] in tensors:
                raise ValueError('未知或重复权重类型')
            length=int(np.prod(weight['shape']))*4
            values=np.frombuffer(raw[offset:offset+length],dtype='<f4')
            if values.size*4!=length or not np.isfinite(values).all():raise ValueError('权重长度或数值无效')
            tensors[weight['name']]={'shape':weight['shape'],'values':values.size}
            offset+=length
        if offset!=len(raw):raise ValueError('模型分片存在额外数据')
    report={'state':'weights_verified_not_converted_or_deployed','created_utc':now(),
        'author_link':'https://github.com/observethesun/coronal_holes#demo',
        'demo':'https://illarionovea.github.io/', 'commit':COMMIT,'model_git_blob':MODEL_BLOB,
        'commit_record_date':'2018-08-11T20:36:20Z','model_source':model_source,'weight_sources':sources,
        'parameter_values':sum(t['values'] for t in tensors.values()),'tensors':tensors,
        'native_input_shape':model['modelTopology']['model_config']['config']['layers'][0]['config']['batch_input_shape'],
        'native_output_layers':model['modelTopology']['model_config']['config']['output_layers'],
        'native_final_activation':model['modelTopology']['model_config']['config']['layers'][-1]['config']['activation'],
        'license_state':'no independent weight redistribution license verified; local scientific evaluation only',
        'training_data_overlap':'training history not fully documented; 2018 commit precedes local 2019/2020 validation but does not establish full data provenance',
        'remaining':['reproduce native browser preprocessing, including first-channel selection and resize coordinates',
                     'numerical parity for graph import, especially transpose SAME padding and output activation',
                     'validation-only input-domain and quality comparison; no original final-test tuning',
                     'resource and licensing checks before any application release'],
        'training_started':False,'test_accessed':False,'candidate_replaced':False}
    atomic_json(output/'snapshot.json',report)
    return report


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment()
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--dataset',required=True);p.add_argument('--tree',required=True)
    a=p.parse_args();r=snapshot(a.output,a.dataset,a.tree)
    print(json.dumps({k:r[k] for k in ('state','parameter_values','native_input_shape','native_final_activation')},ensure_ascii=False))
