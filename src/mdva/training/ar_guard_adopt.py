"""Audited AR precision repair; never overwrite the failed AMP attempt."""
from pathlib import Path
import argparse
import json
import shutil

from .runtime import digest, now, exclusive
from .durable import atomic_json, atomic_checkpoint
from .diagnostic_support import read


def adopt(experiment, evidence):
    from . import fast_train, ar_guard_train, reviewed_train as engine
    from .ar_precision import POLICY
    from .continuation import lock_alive
    from .fast_benchmark import equal
    from .run_locations import run_folder
    experiment, evidence = Path(experiment).resolve(), Path(evidence).resolve()
    if (experiment/'ar-precision-revision.json').exists():
        raise ValueError('精度修订已登记，禁止覆盖')
    if not (experiment/'PAUSE').exists() or any(
        lock_alive(experiment/(n+'.lock')) is not False for n in ('queue','reviewed','watchdog','continuation')
    ):
        raise ValueError('必须先确认旧队列暂停并退出')
    acceptance = read(evidence/'recovery-acceptance-v2.json')
    names = ('fast_data.py','ar_precision.py','ar_guard_train.py')
    code = {n:digest(Path(__file__).parent/n) for n in names}
    if acceptance.get('state') != 'passed' or acceptance['code'] != code:
        raise ValueError('真实失败批次、完整验证及两轮接续尚未通过当前代码验收')
    if not acceptance['source_unchanged'] or not acceptance['migration_numerical_state_exact']:
        raise ValueError('原运行或迁移状态核验未通过')
    approval = read(experiment/'approved-revision.json')
    registry = read(experiment/'run-revisions.json')
    protocol = read(experiment/'protocol.json')
    scope = experiment/'revisions/io-ar-guard-v1'
    if scope.exists():
        raise ValueError('修订目录已有内容；保留并人工核查')
    identifiers = ('AR-A1-s2026','AR-A0-s42','AR-A0-s2026')
    prepared = []
    # Validate all sources and planned destinations before publishing registry.
    for identifier in identifiers:
        if identifier not in approval['order']:
            raise ValueError('修复实验不在原许可范围')
        source = run_folder(experiment,identifier)
        config = read(source.parent.parent/'configs'/(identifier+'.json'))
        if config['execution_revision'] != 'io-fast-v1':
            raise ValueError('预期无损缓存AR配置')
        config = {**config,'execution_revision':'io-ar-guard-v1','ar_precision_policy':POLICY}
        if read(source/'status.json',{}).get('state') == 'completed':
            raise ValueError('已完成运行不允许迁移')
        if identifier == 'AR-A1-s2026':
            for name,h in acceptance['source_hashes'].items():
                if digest(source/name) != h:
                    raise ValueError('故障源文件身份已改变')
            source_saved = engine.load_checkpoint(source/'last.pt')
            if source_saved['identity']['code'] != fast_train.code_identity():
                raise ValueError('原数值源码身份变化')
            trial = evidence/'recovery-trial-v2'
            saved = engine.load_checkpoint(trial/'last.pt')
            best = engine.load_checkpoint(trial/'best.pt')
            identity = read(trial/'identity.json')
            if (saved['identity'] != identity or best['identity'] != identity or identity['config'] != config
                    or identity['code'] != ar_guard_train.code_identity() or saved['epoch'] != 29
                    or saved['epoch'] != len(saved['history']) or best['epoch'] != saved['best_epoch']
                    or identity['data'] != engine.data_identity(protocol['dataset'])):
                raise ValueError('试运行检查点身份/历史不一致')
            def finite(value):
                if engine.torch.is_tensor(value):
                    return bool(engine.torch.isfinite(value).all())
                if isinstance(value,dict):
                    return all(finite(v) for v in value.values())
                if isinstance(value,(tuple,list)):
                    return all(finite(v) for v in value)
                return True
            if not all(finite(value) for value in (saved['model'],saved['optimizer'],best['model'])):
                raise ValueError('试运行参数或优化器非有限')
            payload = (trial,saved,best,identity)
        else:
            if (source/'identity.json').exists():
                raise ValueError('预期尚未启动的AR基线已有状态')
            payload = None
        prepared.append((identifier,source,config,payload))
    with exclusive(Path(__file__).resolve().parents[3]/'.mdva/training-gpu.lock'):
        scope.mkdir(parents=True)
        atomic_json(scope/'source-registry.json',registry)
        configs=[];runs={}
        for identifier,source,config,payload in prepared:
            configs.append(config)
            atomic_json(scope/'configs'/(identifier+'.json'),config)
            destination = scope/'runs'/identifier
            destination.mkdir(parents=True)
            audit = {'created_utc':now(),'source':str(source),'source_status':read(source/'status.json'),
                     'original_attempt_preserved':True,'policy':POLICY,'test_accessed':False,
                     'numerical_policy_changed':True,'not_claimed_amp_equivalent_after_retry':True}
            if payload:
                trial,saved,best,identity = payload
                atomic_checkpoint(destination/'last.pt',saved)
                atomic_checkpoint(destination/'best.pt',best)
                loaded = engine.load_checkpoint(destination/'last.pt')
                if not equal(saved,loaded):
                    raise ValueError('试运行检查点复制改变数值状态')
                atomic_json(destination/'identity.json',identity)
                atomic_json(destination/'metrics.json',saved['history'])
                snapshot=destination/'source';snapshot.mkdir()
                for name in identity['code']:
                    (snapshot/name).write_bytes((Path(__file__).parent/name).read_bytes())
                shutil.copy2(trial/'precision-events.jsonl',destination/'precision-events.jsonl')
                if (trial/'console.log').exists():
                    shutil.copy2(trial/'console.log',destination/'console.log')
                else:
                    lines = ['# 验收轮次来自独立recovery-trial/metrics.json；原故障日志留在io-fast-v1。']
                    lines += [json.dumps({'id':identifier,'epoch':r['epoch'],
                        'dice':r['validation']['dice'],'seconds':r['seconds']},ensure_ascii=False)
                        for r in saved['history'] if r['epoch'] > 27]
                    (destination/'console.log').write_text('\n'.join(lines)+'\n',encoding='utf-8')
                atomic_json(destination/'status.json',{**read(trial/'status.json'),
                    'state':'paused','updated_utc':now(),'reason':'两轮修复验收通过，等待原批准队列接续'})
                audit.update(source_epoch=27,validated_recovery_epochs=[28,29],resume_epoch=30,
                             checkpoint_sha256=digest(destination/'last.pt'),all_trial_state_exact=True)
            atomic_json(destination/'precision-migration-audit.json',audit)
            runs[identifier]=audit
            registry['runs'][identifier]={'relative':'revisions/io-ar-guard-v1',
                                         'entrypoint':'mdva.training.ar_guard_train'}
        atomic_json(scope/'protocol.json',{**protocol,'recognition':configs,'numerical_revision':'io-ar-guard-v1'})
        atomic_json(experiment/'ar-precision-revision.json',{'enabled':True,'acceptance_passed':True,
            'created_utc':now(),'code':code,'runs':runs,'test_unlock':False,
            'acceptance_sha256':digest(evidence/'recovery-acceptance-v2.json'),
            'authorization':'既有继续训练及排查故障授权；修复已复现FP16溢出，原失败版本保留',
            'original_approval_and_order_unchanged':True,'numerical_policy_changed':True,
            'cross_seed_precision_limitation':'此前完成种子为原AMP；须报告本修复及重试次数，不能声称精度策略完全一致'})
        atomic_json(experiment/'run-revisions.json',registry)
    return {'revision':str(scope),'runs':list(runs),'next_epoch':30}


if __name__ == '__main__':
    from .runtime import configure_environment
    configure_environment()
    parser=argparse.ArgumentParser()
    parser.add_argument('--experiment',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(adopt(args.experiment,args.evidence),ensure_ascii=False),flush=True)
