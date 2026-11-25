"""Audited transport to a faster, lossless input pipeline; preserve every old run."""
from pathlib import Path
import argparse
import json

from .durable import atomic_json,atomic_checkpoint
from .runtime import digest,now,exclusive


def adopt(experiment,evidence):
    from . import reviewed_train as old_engine,fast_train
    from .fast_data import DEFAULT_CACHE
    from .fast_benchmark import equal
    from .continuation import lock_alive
    from .run_locations import run_folder
    experiment=Path(experiment).resolve();evidence=Path(evidence).resolve()
    if (experiment/'throughput-revision.json').exists():raise ValueError('效率修订已存在，禁止覆写')
    if not (experiment/'PAUSE').exists() or any(lock_alive(experiment/(name+'.lock')) is not False for name in ('queue','reviewed','watchdog','continuation')):
        raise ValueError('先确认队列安全暂停并退出')
    verification=json.loads((evidence/'fast-tests-result.json').read_text(encoding='utf-8'))
    code={n:digest(Path(__file__).parent/n) for n in ('fast_data.py','fast_train.py')}
    if not verification.get('passed') or verification['code']!=code:raise ValueError('缓存/恢复回归尚未通过当前代码身份')
    benchmarks={c:json.loads((evidence/f'{c}-benchmark.json').read_text(encoding='utf-8')) for c in ('AR','CH')}
    for c,b in benchmarks.items():
        if not b['validation_equal'] or not all(r['exact_payload_equal'] for r in b['trials']):
            raise ValueError('真实模型训练或完整验证不一致')
    approval=json.loads((experiment/'approved-revision.json').read_text(encoding='utf-8'))
    registry=json.loads((experiment/'run-revisions.json').read_text(encoding='utf-8'))
    scope=experiment/'revisions/io-fast-v1'
    if scope.exists():raise ValueError('效率修订目录已存在，请核查未完成迁移，禁止覆盖')
    scope.mkdir(parents=True)
    atomic_json(scope/'source-registry.json',registry)
    protocol=json.loads((experiment/'protocol.json').read_text(encoding='utf-8'))
    runs={};configs=[]
    with exclusive(Path(__file__).resolve().parents[3]/'.mdva/training-gpu.lock'):
        for identifier in approval['order']:
            source=run_folder(experiment,identifier)
            state=json.loads((source/'status.json').read_text(encoding='utf-8')) if (source/'status.json').exists() else {}
            category=identifier.split('-')[0]
            if state.get('state')=='completed' or not benchmarks[category]['adopt']:continue
            original=json.loads((source.parent.parent/'configs'/(identifier+'.json')).read_text(encoding='utf-8'))
            config={**original,'execution_revision':'io-fast-v1','data_workers':benchmarks[category]['chosen']['workers'],
                    'cache_directory':str(DEFAULT_CACHE)}
            if config['micro_batch']!=2 or config['effective_batch']!=8:raise ValueError('优化未获准改变batch或有效batch')
            for key in original:
                if key!='execution_revision' and original[key]!=config[key]:raise ValueError('训练配方发生变化')
            atomic_json(scope/'configs'/(identifier+'.json'),config);configs.append(config)
            run=scope/'runs'/identifier;run.mkdir(parents=True)
            audit={'source':str(source),'old_state':state,'recorded_utc':now(),'new_config':config,
                'batch_and_optimizer_unchanged':True,'test_accessed':False,'benchmark_evidence':str(evidence/f'{category}-benchmark.json')}
            if (source/'identity.json').exists():
                identity=json.loads((source/'identity.json').read_text(encoding='utf-8'))
                if identity['code']!=old_engine.code_identity() or identity['data']!=old_engine.data_identity(protocol['dataset']):
                    raise ValueError('旧运行身份变化，不能迁移')
                saved=old_engine.load_checkpoint(source/'last.pt');best=old_engine.load_checkpoint(source/'best.pt')
                if saved['identity']!=identity or best['identity']!=identity or len(saved['history'])!=saved['epoch']:
                    raise ValueError('检查点身份或完整轮次不一致')
                new_identity={**identity,'config':config,'code':fast_train.code_identity()}
                saved['identity']=new_identity;best['identity']=new_identity
                atomic_checkpoint(run/'last.pt',saved);atomic_checkpoint(run/'best.pt',best)
                reloaded=old_engine.load_checkpoint(run/'last.pt')
                old_saved=old_engine.load_checkpoint(source/'last.pt')
                if not all(equal(old_saved[k],reloaded[k]) for k in old_saved if k!='identity'):
                    raise ValueError('迁移改变了模型/优化器/随机数等数值状态')
                atomic_json(run/'identity.json',new_identity);atomic_json(run/'metrics.json',saved['history'])
                snapshot=run/'source';snapshot.mkdir()
                for name in new_identity['code']:(snapshot/name).write_bytes((Path(__file__).parent/name).read_bytes())
                atomic_json(run/'status.json',{**state,'state':'paused','updated_utc':now(),
                    'reason':'无损输入缓存工程迁移已核验，等待原epoch接续'})
                audit.update(source_last_sha256=digest(source/'last.pt'),source_best_sha256=digest(source/'best.pt'),
                    source_identity=identity,migration_epoch=saved['epoch'],first_fast_epoch=saved['epoch']+1,
                    all_numerical_state_exact=True,old_checkpoints_preserved=True)
            else:audit.update(migration_epoch=0,first_fast_epoch=1)
            atomic_json(run/'cache-migration-audit.json',audit)
            runs[identifier]={'source':str(source),'category':category,'migration_epoch':audit['migration_epoch'],
                'first_fast_epoch':audit['first_fast_epoch'],'data_workers':config['data_workers']}
            registry['runs'][identifier]={'relative':'revisions/io-fast-v1','entrypoint':'mdva.training.fast_train'}
        if not configs:raise ValueError('没有配置达到至少10%的提速门槛，不应用优化')
        atomic_json(scope/'protocol.json',{**protocol,'recognition':configs,'engineering_revision':'io-fast-v1',
            'numerical_recipe_changed':False})
        manifest={'enabled':True,'revision':'io-fast-v1','created_utc':now(),'code':code,'runs':runs,
            'authorization':'用户要求优化训练效率；仅采用已验证无损且至少快10%的输入流水线',
            'benchmark_files':{c:digest(evidence/f'{c}-benchmark.json') for c in benchmarks},
            'regression_evidence_sha256':digest(evidence/'fast-tests-result.json'),
            'test_unlock':False,'original_approval_and_order_unchanged':True,
            'cache_manifests':{f'{c}-{split}':digest(DEFAULT_CACHE/f'{c}-{split}-512/manifest.json') for c in benchmarks for split in ('train','validation')}}
        atomic_json(experiment/'throughput-revision.json',manifest)
        atomic_json(experiment/'run-revisions.json',registry)
    return manifest


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment();p=argparse.ArgumentParser()
    p.add_argument('--experiment',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True)
    args=p.parse_args();print(json.dumps(adopt(args.experiment,args.evidence),ensure_ascii=False),flush=True)
