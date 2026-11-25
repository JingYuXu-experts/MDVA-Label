"""Persistent progress and handoff records, independent of the numerical trainer."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

from .runtime import now
from .durable import atomic_json, commit


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def append(experiment, kind, title, message, *, run_id=None, epoch=None, details=None):
    experiment = Path(experiment)
    timestamp = now()
    identifier = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'-'+uuid.uuid4().hex[:8]
    value = {'id': identifier, 'recorded_utc': timestamp, 'kind': kind, 'title': title,
             'message': message, 'run_id': run_id, 'epoch': epoch, 'details': details or {}}
    atomic_json(experiment/'journal'/f'{identifier}.json', value)
    return value


def entries(experiment, page=1, page_size=12, kind='all'):
    if page < 1 or not 1 <= page_size <= 50:
        raise ValueError('日志页码必须为正数，每页1–50条')
    if kind not in ('all', 'epoch', 'development', 'transition', 'error', 'handoff'):
        raise ValueError('未知日志分类')
    files = sorted((Path(experiment)/'journal').glob('*.json'), reverse=True)
    # Read metadata, never tensors or original image data.
    records = [read(p) for p in files]
    if kind != 'all':
        records = [r for r in records if r['kind'] == kind]
    return {'entries': records[(page-1)*page_size:page*page_size], 'total': len(records),
            'page': page, 'page_size': page_size}


def progress(experiment, protocol=None, runs=None):
    from .run_locations import run_folder
    experiment = Path(experiment)
    protocol = protocol or read(experiment/'protocol.json', {})
    configs = {c['id']: c for c in protocol.get('recognition', [])}
    if runs is None:
        runs = [read(run_folder(experiment, identifier)/'status.json',
                     {'id': identifier, 'state': 'not_started', 'completed_epoch': 0}) for identifier in configs]
    order = read(experiment/'queue.json', {}).get('order', list(configs))
    by_id = {r['id']: r for r in runs}
    coordinator = read(experiment/'continuation.json', {})
    age = None
    if coordinator.get('updated_utc'):
        age = (datetime.now(timezone.utc)-datetime.fromisoformat(coordinator['updated_utc'])).total_seconds()
    live_coordinator = age is not None and age <= 30 and coordinator.get('state') in ('following', 'executing', 'starting')
    active = next((by_id[k] for k in order if k in by_id and by_id[k]['state'] == 'running'), None)
    paused = (experiment/'PAUSE').exists()
    failure = next((by_id[k] for k in order if k in by_id and by_id[k]['state'] in ('failed', 'resource_failed')), None)
    pending = [k for k in order if by_id.get(k, {}).get('state') != 'completed']
    following = next((k for k in pending if not active or k != active['id']), None)
    completed = sum(r['state'] == 'completed' for r in runs)
    result = {'phase': 'M2' if by_id.get('FL-F2-s17', {}).get('state') != 'completed' else 'M3',
              'completed_runs': completed, 'total_runs': len(configs),
              'completed_percent': completed/max(1, len(configs))*100,
              'active': active, 'next_run_id': following, 'paused_requested': paused,
              'automatic_continuation': bool(live_coordinator and not paused),
              'coordinator': coordinator, 'coordinator_age_seconds': age,
              'checkpoint': None, 'epoch_budget': None, 'next_commands': []}
    if active:
        config = configs.get(active['id'], {})
        current_folder = run_folder(experiment, active['id'])
        history = read(current_folder/'metrics.json', [])
        n = active.get('completed_epoch', 0)
        maximum, minimum, patience = config.get('max_epochs', 80), config.get('min_epochs', 20), config.get('patience', 12)
        # This is a conditional estimate, not an invented remaining-time countdown.
        durations = [r['seconds'] for r in history[-3:] if r.get('seconds', 0) > 0]
        typical = sum(durations)/len(durations) if durations else None
        earliest = max(0, minimum-n, patience-(n-active.get('best_epoch', n)))
        checkpoint = current_folder/'last.pt'
        result['checkpoint'] = {'exists': checkpoint.is_file(), 'completed_epoch': n,
                'filename': checkpoint.relative_to(experiment).as_posix(),
                'saved_utc': datetime.fromtimestamp(checkpoint.stat().st_mtime, timezone.utc).isoformat() if checkpoint.is_file() else None}
        result['epoch_budget'] = {'completed': n, 'maximum': maximum, 'minimum': minimum,
                'patience': patience, 'current_percent': n/max(1, maximum)*100,
                'recent_epoch_seconds': typical,
                'earliest_additional_epochs_if_no_improvement': earliest,
                'remaining_upper_seconds': (maximum-n)*typical if typical else None}
    if completed == len(configs) and configs:
        result.update(phase='M5', state='training_completed',
            headline='识别训练队列已完成，等待配置封存与正式测试',
            next_step='核对资源和开发结果，锁定部署配置后再评价识别测试集。')
        result['next_commands'] = [{'label': '生成现有结果报告', 'command': 'python -m mdva.training report'}]
    elif paused:
        result.update(state='pausing' if active else 'paused', headline='已请求在轮次边界保存并暂停',
                      next_step='当前轮次结束后停止；解除暂停后从检查点继续。')
        result['next_commands'] = [{'label': '解除暂停并继续', 'command': 'python -m mdva.training unpause\npython -m mdva.training continue'}]
    elif failure or coordinator.get('state') == 'needs_attention':
        reason = (failure or {}).get('error') or coordinator.get('reason', '检查日志及实验身份')
        result.update(state='needs_attention', headline='需要处理异常后继续', next_step=reason)
        result['next_commands'] = [{'label': '修复原因并核对版本后，单项恢复', 'command': 'python -m mdva.training queue --max-runs 1'},
                                  {'label': '单项恢复后重新接入连续队列', 'command': 'python -m mdva.training continue'}]
    elif active:
        result.update(state='running', headline=f"{active['id']} 正在训练",
            next_step=(f"当前模型完成{'并通过M2检查' if result['phase']=='M2' else ''}后，自动继续 {following}。" if live_coordinator and following else
                       '当前模型完成后，队列按已登记顺序继续。' if live_coordinator else '当前训练仍在运行；连续队列尚未确认接管。'))
        result['next_commands'] = [{'label': '只查看进度，不中断训练', 'command': 'python -m mdva.training status'},
                                  {'label': '请求安全暂停', 'command': 'python -m mdva.training pause'}]
    else:
        result.update(state='waiting' if live_coordinator else 'idle', headline='等待下一项识别实验',
                      next_step=f'下一项为 {pending[0] if pending else "待确认"}，继续时不会重训已经完成的项目。')
        result['next_commands'] = [{'label': '继续连续训练队列', 'command': 'python -m mdva.training continue'}]
    return result


def save_handoff(experiment, value):
    experiment = Path(experiment)
    if value.get('training_plan_revision'):
        from .run_locations import run_folder
        approval=read(experiment/'approved-revision.json',{})
        execution=read(experiment/'reviewed-state.json',{})
        order=approval.get('order',[])
        active=(value.get('active') or {}).get('id') or execution.get('active')
        pending=[identifier for identifier in order if identifier!=active and
                 read(run_folder(experiment,identifier)/'status.json',{}).get('state')!='completed']
        value['next_run_id']=pending[0] if pending else None
        value['phase']='M2' if execution.get('active') in (None,'AR-A0-s17','CH-C1-s17') else 'M3' if active in order[2:6] else 'M4'
    atomic_json(experiment/'checkpoint-progress.json', value)
    if value.get('training_plan_revision'):
        lines=['# 修订训练进度与接续', '', '更新时间UTC：'+now(), '', value['headline'], '',
               f"原协议已完成：{value['completed_runs']}/{value['total_runs']}；3项C0暂缓。", '',
               '当前修订：20261115-R1。每项训练后复算完整验证集、固定训练样本和错误案例；失败停止，最终测试封存。', '',
               value['next_step'], '', '只读状态：python -m mdva.training status', '',
               '经批准的受限队列：python -m mdva.training.reviewed_queue。用户暂停后须用户要求继续；不要执行旧continue。', '',
               '效果记录：修订训练效果分析.md、revision-analysis.json；FL诊断：revisions/io-reviewed-v1/FL-learning-probe/status.json。', '',
               '下一项：'+str(value.get('next_run_id')), '', '## 最近记录','']
        for item in reversed(entries(experiment,page_size=15)['entries']):
            lines += [f"- {item['recorded_utc']}：{item['title']}。{item['message']}"]
        commit(experiment/'训练进度与接续.md',lambda stream:stream.write(('\n'.join(lines)+'\n').encode('utf-8')))
        return
    lines = ['# 本地模型训练进度与接续', '', f'记录时间 UTC：{now()}', '',
             f'当前阶段：{value["phase"]}', '', value['headline'], '',
             f'完成实验：{value["completed_runs"]}/{value["total_runs"]}', '',
             '下一步：'+value['next_step'], '']
    if value.get('active'):
        active = value['active']
        lines += [f'当前实验：{active["id"]}；已完成 {active.get("completed_epoch", 0)} 轮。', '']
    lines += ['## 下一步命令', '', '先激活 Conda mdva311；命令均在本项目根目录执行。', '']
    for item in value['next_commands']:
        lines += [item['label'], '', '```bat', item['command'], '```', '']
    lines += ['应用或电脑重启后，先核对实时状态，再重新接入连续队列：', '',
              '```bat', 'python -m mdva.training status', 'python -m mdva.training continue', '```', '',
              '## 接续约束与剩余工作', '',
              '- 原始数据只读，使用Conda mdva311；单GPU；训练源码、配置或数据身份变化需审计并建立新实验版本。',
              '- 所有识别配置完成后，仍需开发选型、资源实测、封存和正式测试；不依据测试分数回头调参。',
              '- AR原团队对象分组对照、真实模型候选人工审核流程、平台演示与最终验收尚需继续。',
              '- 预测目前仅限原参考目录下的回顾研究；权威事件覆盖、输入历史发布时间及业务发布门槛仍未通过。',
              '- 不自动推送GitHub。完整计划与问题台账可在本页“本地文件与交接资料”查看。', '']
    lines += ['## 最近记录', '']
    for item in reversed(entries(experiment, page_size=30)['entries']):
        lines += [f"- {item['recorded_utc']}：{item['title']}。{item['message']}"]
    lines += ['', '逐轮指标、检查点与所有历史日志留在本实验目录；关闭浏览器不停止后台训练。',
              '异常或资源超限会保留记录并停止接续，不把失败记作完成。', '']
    path = experiment/'训练进度与接续.md'
    commit(path,lambda stream:stream.write('\n'.join(lines).encode('utf-8')))
