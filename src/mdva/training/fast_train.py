"""Identical reviewed numerical engine with a separately identified input pipeline."""
from __future__ import annotations

import argparse
from functools import partial
import json
from pathlib import Path

from . import reviewed_train as engine
from .fast_data import CachedDataset,loader_for
from .runtime import digest
from .durable import atomic_json

_base_identity=engine.code_identity
architecture=engine.architecture


def code_identity():
    return {**_base_identity(),**{n:digest(Path(__file__).parent/n) for n in ('fast_data.py','fast_train.py')}}


def train(config,dataset,run,resume=False,**kwargs):
    if config.get('execution_revision')!='io-fast-v1':raise ValueError('必须使用独立缓存工程身份')
    engine.validate_config(config)
    run=engine.output_path(run,dataset)
    run.mkdir(parents=True,exist_ok=True)
    cache=Path(config['cache_directory']).resolve()
    if (run/'identity.json').exists():
        expected={'config':config,'code':code_identity(),'data':engine.data_identity(dataset),
                  'device':kwargs.get('device','cuda'),'diagnostic_limit':kwargs.get('diagnostic_limit')}
        if json.loads((run/'identity.json').read_text(encoding='utf-8'))!=expected:
            raise ValueError('缓存执行身份不同，不能修改旧运行')
    prior=json.loads((run/'status.json').read_text(encoding='utf-8')) if (run/'status.json').exists() else {}
    atomic_json(run/'status.json',{**prior,'id':config['id'],'state':'running','task_phase':'verify_lossless_cache',
        'pid':engine.os.getpid(),'updated_utc':engine.now(),'completed_epoch':prior.get('completed_epoch',0),
        'best_epoch':prior.get('best_epoch',0),'best_validation_dice':prior.get('best_validation_dice',-1.)})
    old=(engine.code_identity,engine.SegmentationDataset,engine.loader_for)
    try:
        engine.code_identity=code_identity
        engine.SegmentationDataset=partial(CachedDataset,cache_root=cache)
        engine.loader_for=loader_for
        return engine.train(config,dataset,run,resume,**kwargs)
    except Exception as exc:
        status=json.loads((run/'status.json').read_text(encoding='utf-8'))
        if status.get('state') not in ('failed','resource_failed'):
            atomic_json(run/'status.json',{**status,'state':'failed','updated_utc':engine.now(),'error':str(exc),
                        'recovery':'保留完整检查点；先排查缓存/初始化与身份，不绕过核验'})
        raise
    finally:
        engine.code_identity,engine.SegmentationDataset,engine.loader_for=old


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment()
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);p.add_argument('--dataset',type=Path,required=True)
    p.add_argument('--run',type=Path,required=True);p.add_argument('--resume',action='store_true');p.add_argument('--stop-after',type=int)
    args=p.parse_args();config=json.loads(args.config.read_text(encoding='utf-8'))
    print(json.dumps(train(config,args.dataset,args.run,args.resume,stop_after=args.stop_after),ensure_ascii=False),flush=True)
