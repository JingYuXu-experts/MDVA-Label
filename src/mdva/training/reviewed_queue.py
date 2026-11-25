"""Approved R1 allowlist, engineering migration and mandatory between-run review."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from .durable import atomic_json, commit
from .diagnostic_support import read
from .runtime import digest, exclusive, now

ROOT=Path(__file__).resolve().parents[3]
DEFAULT=ROOT/'out/training-20261112-v2'


def prepare(experiment):
    from . import reviewed_train as trainer
    from .run_locations import location
    from .reviewed_probe import prepare_review
    experiment=Path(experiment).resolve()
    if (experiment/'approved-revision.json').exists():
        return read(experiment/'approved-revision.json')
    if not (experiment/'PAUSE').exists():raise ValueError('先暂停再准备工程修订')
    evidence=read(experiment/'gates/M1-reviewed.json',{})
    if not evidence.get('passed'):raise ValueError('M1恢复/保存检查尚未通过')
    proposal=read(ROOT/'experiments/training/recognition-revision-20261115-proposal.json')
    for relative,expected in proposal['original_files_sha256'].items():
        if digest(ROOT/relative)!=expected:raise ValueError('原计划基线变化，请先核查：'+relative)
    scope=experiment/'revisions/io-reviewed-v1';scope.mkdir(parents=True,exist_ok=True)
    registry=read(experiment/'run-revisions.json',{'runs':{}})
    original_registry=read(scope/'original-run-registry.json',copy.deepcopy(registry))
    configs=[];legacy=None
    for identifier in proposal['proposed_execution_order']:
        entry=original_registry['runs'].get(identifier)
        old_scope=experiment if not entry else (experiment/entry['relative']).resolve()
        if old_scope!=experiment and old_scope!=experiment/'revisions/ch-fp32-v1':
            raise ValueError('旧修订来源不在登记范围')
        config=read(old_scope/'configs'/(identifier+'.json'))
        if not config:raise ValueError('缺少原冻结配置：'+identifier)
        config={**config,'execution_revision':'io-reviewed-v1'}
        if config['category']=='CH' and config.get('cnn193_compute')!='fp32':raise ValueError('CH必须统一FP32')
        configs.append(config);atomic_json(scope/'configs'/(identifier+'.json'),config)
        if identifier=='AR-A0-s17':legacy=old_scope/'runs'/identifier
        registry['runs'][identifier]={'relative':'revisions/io-reviewed-v1','entrypoint':'mdva.training.reviewed_train'}
    protocol=read(experiment/'protocol.json')
    atomic_json(scope/'protocol.json',{**protocol,'recognition':configs,'engineering_revision':'io-reviewed-v1'})
    # Explicit transport to a new identity, without changing numerical state or the failed source.
    old=read(legacy/'identity.json');saved=trainer.load_checkpoint(legacy/'last.pt')
    best=trainer.load_checkpoint(legacy/'best.pt')
    if saved['identity']!=old or best['identity']!=old:raise ValueError('原检查点身份不一致')
    from .runtime import code_identity
    if old['code']!=code_identity() or old['data']!=trainer.data_identity(Path(protocol['dataset'])):
        raise ValueError('旧数值代码或数据变化；不得跨身份迁移')
    if len(saved['history'])!=saved['epoch'] or best['epoch']!=saved['best_epoch']:
        raise ValueError('检查点历史或最佳轮次不一致')
    if not all(trainer.torch.isfinite(v).all() for v in saved['model'].values() if trainer.torch.is_floating_point(v)):
        raise ValueError('原检查点出现非有限参数')
    migrated=scope/'runs/AR-A0-s17';migrated.mkdir(parents=True,exist_ok=True)
    identity={**old,'config':configs[0],'code':trainer.code_identity()}
    audit={'created_utc':now(),'old_run':str(legacy),'old_identity':old,'new_identity':identity,
           'original_status':read(legacy/'status.json'),'last_sha256':digest(legacy/'last.pt'),
           'best_sha256':digest(legacy/'best.pt'),'epoch':saved['epoch'],'best_epoch':saved['best_epoch'],
           'bad_epochs':saved['bad_epochs'],'numerical_state_changed':False,
           'expected_finish_without_extra_epoch':trainer.early_stop_met(saved['epoch'],saved['bad_epochs'],identity['config']),
           'reason':'仅原子保存与恢复前早停工程修复；原失败运行、权重与数据均保留，独立工程身份'}
    atomic_json(migrated/'migration-audit.json',audit)
    saved['identity']=identity;best['identity']=identity
    trainer.atomic_checkpoint(migrated/'last.pt',saved);trainer.atomic_checkpoint(migrated/'best.pt',best)
    atomic_json(migrated/'identity.json',identity);atomic_json(migrated/'metrics.json',saved['history'])
    snapshot=migrated/'source';snapshot.mkdir(exist_ok=True)
    for name in identity['code']:(snapshot/name).write_bytes((Path(__file__).parent/name).read_bytes())
    atomic_json(migrated/'status.json',{'id':'AR-A0-s17','state':'paused','completed_epoch':saved['epoch'],
        'best_epoch':saved['best_epoch'],'best_validation_dice':saved['best'],'updated_utc':now(),
        'reason':'经审计的独立工程修订迁移，等待正常恢复收尾'})
    atomic_json(scope/'original-run-registry.json',original_registry)
    atomic_json(experiment/'run-revisions.json',registry)
    prepare_review(experiment)
    output=ROOT/'out/model-diagnostics/20261115-04'
    source=ROOT/'out/model-diagnostics/20261115-03';output.mkdir(parents=True,exist_ok=True)
    def link(source_file,destination):
        if Path(destination).exists():
            if digest(Path(source_file))!=digest(Path(destination)):
                raise ValueError('已有缓存与源报告不同，拒绝混用')
        else:os.link(source_file,destination)
        return destination
    for name in ('cases','probabilities','assets','models'):
        shutil.copytree(source/name,output/name,copy_function=link,dirs_exist_ok=True)
    for name in ('samples.json','audit.json'):
        shutil.copy2(source/name,output/name)
    # The migrated AR checkpoint has a new serialized identity: never reuse its old scores as fresh evidence.
    for name in ('cases','probabilities'):
        target=(output/name/'AR-A0-s17').resolve()
        if not target.is_relative_to(output.resolve()):raise ValueError('诊断缓存清理越界')
        if target.exists():shutil.rmtree(target)
    (output/'models/AR-A0-s17.json').unlink(missing_ok=True)
    approval={'revision':'20261115-R1','approved_utc':now(),
        'authorization':'用户明确要求按照改进后的方法继续完成训练，并逐项分析有效性',
        'scope':str(scope),'order':proposal['proposed_execution_order'], 'diagnostic_output':str(output),
        'gpu_budget_seconds':40*3600,'test_unlock':False,'FL_formal_candidates_approved':[],
        'code':{n:digest(Path(__file__).parent/n) for n in ('reviewed_queue.py','reviewed_analysis.py','reviewed_probe.py',
                  'reviewed_train.py','durable.py')},'original_protocol_sha256':digest(experiment/'protocol.json'),
        'original_queue_sha256':digest(experiment/'queue.json')}
    atomic_json(experiment/'approved-revision.json',approval)
    atomic_json(output/'identity.json',{'parent_report':'20261115-03','engineering_revision':approval,'test_accessed':False})
    from .reviewed_analysis import publish_running
    publish_running(output)
    atomic_json(output/'status.json',{'state':'running','phase':'reviewed_training','updated_utc':now(),
        'task_type':'reviewed_training_queue','training_stopped':False,
        'message':'修订训练已获准；保留29个完整运行，后续逐项加入新的复算结果',
        'models_completed':29,'models_total':39})
    atomic_json(experiment/'diagnostic-hold.json',{'resolved_by':'20261115-R1 approval','requires_user_confirmation':False,
        'reason':'用户已授权，工程门槛通过后按受限队列执行；独立测试仍封存','automatic_restart':False})
    return approval


def run(experiment):
    from .run_locations import location,run_folder
    from .journal import append,progress,save_handoff
    from .continuation import observe
    experiment=Path(experiment).resolve();approval=read(experiment/'approved-revision.json')
    if not approval:raise ValueError('先完成M1并显式准备获准修订')
    for name,expected in approval['code'].items():
        if digest(Path(__file__).parent/name)!=expected:raise ValueError('执行代码变化，需审计新修订：'+name)
    for name,key in [('protocol.json','original_protocol_sha256'),('queue.json','original_queue_sha256')]:
        if digest(experiment/name)!=approval[key]:raise ValueError('原协议/队列变化，停止')
    child=None
    def pause(*_): (experiment/'PAUSE').touch()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,pause)
    with exclusive(experiment/'reviewed.lock'),exclusive(experiment/'queue.lock'):
        def update(state,message,active=None):
            value={'state':state,'reason':message,'updated_utc':now(),'pid':os.getpid(),
                   'active':active,'child_pid':child.pid if child else None,'revision':'20261115-R1',
                   'scope':'仅已批准9次新增及1个中断收尾；不启动C0/新FL/预测/测试'}
            atomic_json(experiment/'reviewed-state.json',value)
            atomic_json(experiment/'continuation.json',value)
            if state!='analyzing':
                report_output=Path(approval['diagnostic_output'])
                atomic_json(report_output/'status.json',{'state':'running' if state=='executing' else
                    'completed' if state=='completed' else 'failed' if state=='needs_attention' else 'paused',
                    'updated_utc':now(),'phase':'reviewed_training','task_type':'reviewed_training_queue',
                    'training_stopped':state not in ('executing','analyzing'),'message':message,
                    'models_completed':len(list((report_output/'models').glob('*.json'))),'models_total':39})
            p=progress(experiment);p.update(training_plan_revision='20261115-R1',headline=message,
                next_step='每项训练后必须完成验证复算及效果分析；异常停止留证；测试集封存。',
                next_commands=[{'label':'查询状态','command':'python -m mdva.training status'}],
                automatic_continuation=state in ('executing','analyzing'))
            save_handoff(experiment,p)
        def execute(args,log,active=None):
            nonlocal child
            with log.open('a',encoding='utf-8') as stream:
                child=subprocess.Popen([sys.executable,'-u','-m',*args],cwd=ROOT,stdin=subprocess.DEVNULL,
                    stdout=stream,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                while child.poll() is None:
                    if (experiment/'PAUSE').exists() and active:
                        (run_folder(experiment,active)/'PAUSE').touch()
                    observe(experiment)
                    update('analyzing' if args[0].endswith('analysis') else 'executing',
                           ('正在分析效果：' if args[0].endswith('analysis') else '按修订计划执行：')+(active or 'FL训练样本学习检查'),active)
                    time.sleep(5)
                result=child.returncode;child=None
                return result
        try:
            if (experiment/'PAUSE').exists():update('paused','用户暂停，检查点保留');return
            probe=Path(approval['scope'])/'FL-learning-probe';probe.mkdir(parents=True,exist_ok=True)
            code=execute(['mdva.training.reviewed_probe','--experiment',str(experiment)],probe/'console.log')
            if code:raise RuntimeError('FL学习检查异常，见其console.log')
            if (experiment/'PAUSE').exists():update('paused','已按用户要求保存并暂停');return
            probe_result=read(probe/'status.json')
            append(experiment,'development','FL小样本学习检查结束',probe_result['finding'],details=probe_result)
            used=0.
            for identifier in approval['order']:
                if (experiment/'PAUSE').exists():update('paused','用户暂停，不启动下一项');return
                scope,entry=location(experiment,identifier);folder=run_folder(experiment,identifier)
                config=read(scope/'configs'/(identifier+'.json'))
                if config!=next(c for c in read(scope/'protocol.json')['recognition'] if c['id']==identifier):
                    raise ValueError('修订配置与协议不同')
                state=read(folder/'status.json',{})
                if state.get('state')!='completed':
                    folder.mkdir(parents=True,exist_ok=True)
                    args=[entry,'--config',str(scope/'configs'/(identifier+'.json')),
                          '--dataset',read(scope/'protocol.json')['dataset'],'--run',str(folder)]
                    if (folder/'identity.json').exists():args.append('--resume')
                    atomic_json(experiment/'queue-state.json',{'state':'running','active':identifier,'updated_utc':now()})
                    append(experiment,'transition','开始修订实验：'+identifier,'本项回答预注册基线或种子稳定性问题；完成后自动分析，未通过则停止。')
                    code=execute(args,folder/'console.log',identifier)
                    if (experiment/'PAUSE').exists():update('paused','当前完整轮次已保存，用户暂停');return
                    state=read(folder/'status.json',{})
                    if code or state.get('state')!='completed':raise RuntimeError(identifier+'训练异常，停止后续实验')
                used+=state.get('resource',{}).get('seconds',0)
                review=read(experiment/'revision-analysis.json',{}).get('runs',{}).get(identifier)
                if not review or not review.get('continue'):
                    code=execute(['mdva.training.reviewed_analysis','--experiment',str(experiment),'--id',identifier],
                                 folder/'analysis-console.log',identifier)
                    if code:raise RuntimeError(identifier+'效果分析未通过，保留原因并停止')
                    review=read(experiment/'revision-analysis.json')['runs'][identifier]
                    append(experiment,'development',identifier+'效果分析完成',review['finding'],details=review)
                if used>approval['gpu_budget_seconds']:raise RuntimeError('已达40GPU小时计划预算，停止并重新估算')
            atomic_json(experiment/'queue-state.json',{'state':'idle','updated_utc':now(),'reason':'获准范围完成，C0/FL扩展/测试未启动'})
            (experiment/'PAUSE').touch()
            update('completed','获准AR/CH训练与逐项分析完成；FL及测试等待相应证据与验收')
            append(experiment,'handoff','修订训练获准范围完成','请查看修订训练效果分析；保留未通过项目，不自动扩展或开放测试。')
        except Exception as exc:
            (experiment/'PAUSE').touch()
            update('needs_attention',str(exc))
            append(experiment,'error','修订训练停止排查',str(exc))
            raise


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment();parser=argparse.ArgumentParser()
    parser.add_argument('--experiment',type=Path,default=DEFAULT);parser.add_argument('--prepare',action='store_true')
    args=parser.parse_args()
    if args.prepare:print(json.dumps(prepare(args.experiment),ensure_ascii=False),flush=True)
    else:run(args.experiment)
