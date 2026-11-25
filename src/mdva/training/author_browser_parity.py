"""Verify saved offline original TFJS outputs against the pure-array importer."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from .author_tfjs import load,native_binary
from .durable import atomic_json
from .runtime import digest,now


def verify(root):
    import torch
    root=Path(root).resolve();model,_=load(root/'keras');torch.set_num_threads(4)
    rows=[]
    with torch.inference_mode():
        for folder in ('parity-01','parity-02'):
            directory=root/folder
            native=json.loads((directory/'native-results.json').read_text(encoding='utf-8'))
            if native['runtime']['backend']!='cpu' or native['external_requests']!=0:raise ValueError('参考运行时不是离线CPU')
            cases=json.loads((directory/'inputs.json').read_text(encoding='utf-8'))['cases']
            for case in cases:
                size=case['size'];x=np.fromfile(directory/case['input'],dtype='<f4').reshape(1,size,size,1)
                _,captures=model(torch.from_numpy(x.transpose(0,3,1,2).copy()),capture=case['capture'],synthetic=size!=256)
                for name,y in captures.items():
                    record=next(r for r in native['results'] if r['id']==case['id'] and r['name']==name)
                    file=directory/record['file']
                    if digest(file)!=record['sha256']:raise ValueError('原运行时结果改变')
                    actual=np.fromfile(file,dtype='<f4').reshape(record['shape'])
                    converted=y.numpy().transpose(0,2,3,1)
                    passed=bool(np.allclose(actual,converted,atol=2e-5,rtol=2e-5))
                    row={'case':case['id'],'layer':name,'shape':record['shape'],
                         'input_sha256':digest(directory/case['input']),'reference_sha256':digest(file),
                         'max_absolute_difference':float(abs(actual-converted).max()),
                         'mean_absolute_difference':float(abs(actual-converted).mean()),'allclose':passed}
                    if name=='conv2d_95':
                        row['native_binary_mismatches']=int((native_binary(actual)!=native_binary(converted)).sum())
                        passed &= row['native_binary_mismatches']==0
                    row['passed']=bool(passed);rows.append(row)
    cache=root.parents[1]/'recognition-improvement-20261116/author-browser-01'
    first=json.loads((cache/'input-cache.json').read_text(encoding='utf-8'))['rows'][0]
    reference_input=root/'parity-02/image256.input.f32'
    first_case=json.loads((root/'parity-02/inputs.json').read_text(encoding='utf-8'))['cases'][0]
    preprocessing_exact=first['uri']==first_case['uri'] and (cache/first['file']).read_bytes()==reference_input.read_bytes()
    report={'state':'completed','updated_utc':now(),'passed':all(r['passed'] for r in rows) and preprocessing_exact,
        'atol':2e-5,'rtol':2e-5,'rows':rows,'input_preprocessing_exact':preprocessing_exact,
        'importer_sha256':digest(Path(__file__).with_name('author_tfjs.py')),
        'verifier_sha256':digest(__file__),'native_script_sha256':digest(root/'native-parity.cjs'),
        'native_runtime_sha256':digest(root/'index.js'),'snapshot_sha256':digest(root/'keras/snapshot.json'),
        'native_model_commit':'a73a848bbc7eb5ef012daa9373a83fd7ee8cfd9a',
        'scope':'Two synthetic32 inputs, intermediate and output layers; one real CH validation256 input. CPU FP32 only.',
        'not_proven':['scientific accuracy','weight license','all-input bitwise identity','GPU numerical identity','application readiness']}
    if (root/'numerical-parity.json').exists():raise ValueError('数值核验报告已存在，不覆盖')
    atomic_json(root/'numerical-parity.json',report)
    return report


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment()
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);a=p.parse_args()
    r=verify(a.source);print(json.dumps({'passed':r['passed'],'max_difference':max(v['max_absolute_difference'] for v in r['rows'])},ensure_ascii=False))
