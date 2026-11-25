"""Isolated author-graph experiment; never edits or resumes the original 42 runs."""
from __future__ import annotations

import argparse
from functools import partial
import json
from pathlib import Path

from . import reviewed_train as engine
from .author_cnn193 import architecture
from .fast_data import CachedDataset,loader_for
from .runtime import digest

_base_identity=engine.code_identity


def code_identity():
    return {**_base_identity(),**{n:digest(Path(__file__).parent/n) for n in
        ('fast_data.py','author_cnn193.py','author_train.py')}}


def train(config,dataset,run,resume=False,**kwargs):
    if config.get('study')!='author-graph-20261116' or config['id']!='CH-H0-s2026':
        raise ValueError('只允许独立的作者结构假设实验')
    if config['category']!='CH' or config['model']!='cnn193_author':raise ValueError('实验任务不符')
    original=(engine.architecture,engine.code_identity,engine.SegmentationDataset,engine.loader_for)
    try:
        engine.architecture=architecture;engine.code_identity=code_identity
        engine.SegmentationDataset=partial(CachedDataset,cache_root=Path(config['cache_directory']))
        engine.loader_for=loader_for
        return engine.train(config,dataset,run,resume,**kwargs)
    finally:
        engine.architecture,engine.code_identity,engine.SegmentationDataset,engine.loader_for=original


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment()
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--dataset',type=Path,required=True);parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--resume',action='store_true');parser.add_argument('--stop-after',type=int,default=3)
    parser.add_argument('--diagnostic-limit',type=int,choices=[16])
    args=parser.parse_args();config=json.loads(args.config.read_text(encoding='utf-8'))
    print(json.dumps(train(config,args.dataset,args.run,args.resume,stop_after=args.stop_after,
        diagnostic_limit=args.diagnostic_limit),ensure_ascii=True),flush=True)
