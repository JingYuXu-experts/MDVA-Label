"""Paired C2 warm-start fine-tuning, isolated from frozen original runs."""
from __future__ import annotations

import argparse
from functools import partial
import json
from pathlib import Path

import torch
from safetensors.torch import load_file

from . import reviewed_train as engine
from .cnn193_fp32 import architecture as baseline
from .fast_data import CachedDataset,loader_for
from .regional_loss import loss_value
from .runtime import digest

_base_identity=engine.code_identity


def code_identity():
    return {**_base_identity(),**{n:digest(Path(__file__).parent/n) for n in
        ('fast_data.py','regional_train.py','regional_loss.py')}}


def architecture(config):
    path=Path(config['initial_weights'])
    if digest(path)!=config['initial_weights_sha256']:raise ValueError('共同起点权重身份变化')
    model,provenance=baseline(config)
    weights=load_file(str(path))
    if any(not torch.isfinite(t).all() for t in weights.values()):raise ValueError('共同起点非有限')
    model.load_state_dict(weights)
    return model,{**provenance,'initial_weights_sha256':config['initial_weights_sha256'],
                  'experiment':'paired local connected-region loss hypothesis, not author reproduction'}


def train(config,dataset,run,resume=False,**kwargs):
    expected={'CH-R0-s2026':'control','CH-R1-s2026':'region_weighted'}
    if config.get('study')!='region-loss-20261116' or expected.get(config['id'])!=config.get('region_mode'):
        raise ValueError('未登记的区域损失对照')
    if config['category']!='CH' or config['loss']!='bce_dice' or config.get('fine_tune_lr')!=1e-4:
        raise ValueError('对照条件不匹配')
    original=(engine.architecture,engine.code_identity,engine.SegmentationDataset,engine.loader_for,
              engine.loss_value,engine.torch)
    def adam(*args,**opts):
        opts['lr']=config['fine_tune_lr']
        return torch.optim.Adam(*args,**opts)
    class OptimProxy:
        Adam=staticmethod(adam)
        def __getattr__(self,name):return getattr(torch.optim,name)
    class TorchProxy:
        optim=OptimProxy()
        def __getattr__(self,name):return getattr(torch,name)
    try:
        engine.architecture=architecture;engine.code_identity=code_identity
        engine.SegmentationDataset=partial(CachedDataset,cache_root=Path(config['cache_directory']))
        engine.loader_for=loader_for
        engine.loss_value=loss_value if config['region_mode']=='region_weighted' else original[4]
        engine.torch=TorchProxy()
        return engine.train(config,dataset,run,resume,**kwargs)
    finally:
        (engine.architecture,engine.code_identity,engine.SegmentationDataset,engine.loader_for,
         engine.loss_value,engine.torch)=original


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment()
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);p.add_argument('--dataset',type=Path,required=True)
    p.add_argument('--run',type=Path,required=True);p.add_argument('--resume',action='store_true');p.add_argument('--stop-after',type=int,default=3)
    a=p.parse_args();config=json.loads(a.config.read_text(encoding='utf-8'))
    print(json.dumps(train(config,a.dataset,a.run,a.resume,stop_after=a.stop_after),ensure_ascii=True),flush=True)
