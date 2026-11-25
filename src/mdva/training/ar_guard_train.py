"""Independent numerical revision retaining the lossless cached input engine."""
from __future__ import annotations

import argparse
from functools import partial
import json
import os
from pathlib import Path

from . import reviewed_train as engine
from .ar_precision import architecture
from .fast_data import CachedDataset, loader_for
from .durable import atomic_json
from .runtime import digest, now


def code_identity():
    return {**_base_identity(), **{name: digest(Path(__file__).parent/name)
        for name in ('fast_data.py', 'ar_precision.py', 'ar_guard_train.py')}}


_base_identity = engine.code_identity


def train(config, dataset, run, resume=False, **kwargs):
    if config.get('execution_revision') != 'io-ar-guard-v1':
        raise ValueError('必须使用独立AR精度修复身份')
    engine.validate_config(config)
    run = engine.output_path(run, dataset)
    run.mkdir(parents=True, exist_ok=True)
    old = (engine.code_identity, engine.SegmentationDataset, engine.loader_for, engine.architecture)

    def log(event):
        with (run/'precision-events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({**event, 'updated_utc': now()}, ensure_ascii=False)+'\n')
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps({'precision_retry': event}, ensure_ascii=False), flush=True)

    try:
        engine.code_identity = code_identity
        engine.SegmentationDataset = partial(CachedDataset, cache_root=Path(config['cache_directory']))
        engine.loader_for = loader_for
        engine.architecture = partial(architecture, on_retry=log)
        return engine.train(config, dataset, run, resume, **kwargs)
    except Exception as exc:
        # Engine identity rejection must never overwrite an existing run.
        expected = {'config': config, 'code': code_identity(), 'data': engine.data_identity(dataset),
                    'device': kwargs.get('device', 'cuda'), 'diagnostic_limit': kwargs.get('diagnostic_limit')}
        identity = json.loads((run/'identity.json').read_text(encoding='utf-8')) if (run/'identity.json').exists() else None
        if identity == expected:
            state = json.loads((run/'status.json').read_text(encoding='utf-8')) if (run/'status.json').exists() else {}
            if state.get('state') not in ('failed', 'resource_failed'):
                atomic_json(run/'status.json', {**state, 'state': 'failed', 'updated_utc': now(), 'error': str(exc)})
        raise
    finally:
        engine.code_identity, engine.SegmentationDataset, engine.loader_for, engine.architecture = old


if __name__ == '__main__':
    from .runtime import configure_environment
    configure_environment()
    p = argparse.ArgumentParser()
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--dataset', type=Path, required=True)
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--resume', action='store_true')
    p.add_argument('--stop-after', type=int)
    args = p.parse_args()
    print(json.dumps(train(json.loads(args.config.read_text(encoding='utf-8')), args.dataset,
                           args.run, args.resume, stop_after=args.stop_after), ensure_ascii=False), flush=True)
