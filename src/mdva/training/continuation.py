"""Follow an existing run, then continue the registered GPU queue without overlap."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

from .journal import append, progress, read, save_handoff
from .runtime import digest, exclusive, now
from .durable import atomic_json


def lock_alive(path):
    """Unknown ownership is treated conservatively; never steal an unverified lock."""
    import psutil
    if not Path(path).exists():
        return False
    try:
        owner = read(path)
        return abs(psutil.Process(owner['pid']).create_time()-owner['birth']) < .01
    except psutil.NoSuchProcess:
        return False
    except (OSError, ValueError, KeyError, psutil.AccessDenied):
        return None


def m2_gate(experiment):
    experiment = Path(experiment)
    run = experiment/'runs/FL-F2-s17'
    state, history = read(run/'status.json', {}), read(run/'metrics.json', [])
    diagnostic = read(experiment/'diagnostics/resume-verification.json', {})
    resource = state.get('resource', {})
    protocol = read(experiment/'protocol.json', {})
    config = next((c for c in protocol.get('recognition', []) if c['id'] == 'FL-F2-s17'), {})
    checks = {
        'formal_run_completed': state.get('state') == 'completed' and state.get('formal') is True,
        'checkpoints_present': (run/'last.pt').is_file() and (run/'best.pt').is_file(),
        'history_matches_completed_epoch': bool(history) and len(history) == state.get('completed_epoch'),
        'minimum_training_epochs': state.get('completed_epoch', 0) >= config.get('min_epochs', 20),
        'finite_validation': bool(history) and all(
            isinstance(r.get('validation', {}).get(k), (float, int)) and math.isfinite(r['validation'][k])
            for r in history for k in ('dice', 'loss')),
        'learning_observed': state.get('best_validation_dice', 0) > 0 and bool(history) and
            min(r['train_loss'] for r in history) < history[0]['train_loss'],
        'resource_limits': resource.get('peak_reserved_gib', float('inf')) <= 6 and
            resource.get('peak_rss_gib', float('inf')) <= 20 and
            resource.get('peak_total_vram_used_gib', float('inf')) <= 7.5,
        'exact_resume_verified': diagnostic.get('passed') is True and diagnostic.get('max_weight_difference') == 0,
    }
    return {'phase': 'M2', 'passed': all(checks.values()), 'checks': checks,
            'checked_utc': now(), 'purpose': '实验工程门槛，不代表模型精度已达发布标准',
            'metrics_sha256': digest(run/'metrics.json') if history else None}


def observe(experiment):
    """One journal event per observed epoch/state change; not one event per poll."""
    experiment = Path(experiment)
    from .run_locations import run_folder
    protocol = read(experiment/'protocol.json')
    seen = read(experiment/'journal-seen.json', {})
    changed = False
    for config in protocol['recognition']:
        identifier = config['id']
        state = read(run_folder(experiment, identifier)/'status.json')
        if not state:
            continue
        previous = seen.get(identifier, {})
        n = state.get('completed_epoch', 0)
        if previous.get('state') != state['state']:
            if state['state'] == 'running':
                append(experiment, 'transition', f'{identifier} 训练运行中',
                       f'已完成{n}轮；关闭浏览器不会中断训练。', run_id=identifier, epoch=n)
            elif state['state'] == 'completed':
                append(experiment, 'transition', f'{identifier} 训练完成',
                       f"完成{n}轮，最佳开发Dice {state.get('best_validation_dice', 0):.4f}；不是正式测试结论。",
                       run_id=identifier, epoch=n, details={'reason': state.get('reason'), 'resource': state.get('resource')})
            elif state['state'] in ('failed', 'resource_failed', 'paused'):
                append(experiment, 'error' if state['state'] != 'paused' else 'handoff', f'{identifier} 暂停接续',
                       state.get('error') or state.get('reason', '已保存完整轮次，等待继续'), run_id=identifier, epoch=n)
            changed = True
        if n > previous.get('epoch', 0):
            history = read(run_folder(experiment, identifier)/'metrics.json', [])
            metric = next((r for r in reversed(history) if r['epoch'] == n), {})
            label = '接入时已有进度' if not previous else f'已完成第{n}轮'
            value = metric.get('validation', {}).get('dice')
            text = f'完整检查点已保存；开发Dice {value:.4f}。' if value is not None else '完整轮次状态已写入本地。'
            append(experiment, 'epoch', f'{identifier} {label}', text,
                   run_id=identifier, epoch=n, details={'metric': metric,
                       'note': '记录时间是观察/补录时间；原逐轮文件未包含的历史时刻不作推算'})
            changed = True
        seen[identifier] = {'state': state['state'], 'epoch': n}
    if changed:
        atomic_json(experiment/'journal-seen.json', seen)
    return changed


def run(experiment, poll_seconds=5):
    experiment = Path(experiment).resolve()
    child = None
    stream = None
    with exclusive(experiment/'continuation.lock'):
        prior_state = None
        def update(state, reason):
            nonlocal prior_state
            value = {'state': state, 'reason': reason, 'updated_utc': now(), 'pid': os.getpid(),
                     'scope': '全部已登记识别训练；单GPU；失败和用户暂停时停止接续',
                     'child_pid': child.pid if child else None}
            atomic_json(experiment/'continuation.json', value)
            changed = state != prior_state
            if changed:
                append(experiment, 'error' if state == 'needs_attention' else 'handoff',
                       {'following': '连续队列已接管', 'executing': '连续队列正在运行',
                        'paused': '已暂停连续训练', 'completed': '全部识别训练结束',
                        'needs_attention': '连续队列需要处理异常', 'starting': '正在启动下一段队列'}.get(state, state), reason)
                prior_state = state
            if observe(experiment) or changed:
                save_handoff(experiment, progress(experiment))
        try:
            update('starting', '已开启持续接续；先核对现有进程与本地检查点。')
            while True:
                if (experiment/'PAUSE').exists():
                    active = read(experiment/'queue-state.json', {}).get('active')
                    registered = {c['id'] for c in read(experiment/'protocol.json')['recognition']}
                    if active in registered:
                        from .run_locations import run_folder
                        marker = run_folder(experiment, active)/'PAUSE'
                        if marker.parent.is_dir():
                            marker.touch(exist_ok=True)
                    if child and child.poll() is None:
                        # The CLI pause command writes the active run's PAUSE marker.
                        # Keep observing until the existing queue has saved and stopped.
                        update('following', '等待当前轮次结束并保存，再响应用户暂停。')
                        time.sleep(poll_seconds)
                        continue
                    if lock_alive(experiment/'queue.lock') is not False:
                        update('following', '已收到暂停请求，等待现有队列保存当前轮次。')
                        time.sleep(poll_seconds)
                        continue
                    update('paused', '用户暂停；检查点和进度保留，解除暂停后从原轮次继续。')
                    return
                if child and child.poll() is None:
                    update('executing', '按冻结顺序执行，已完成的实验自动跳过。')
                    time.sleep(poll_seconds)
                    continue
                if child:
                    code = child.returncode
                    child = None
                    if stream:
                        stream.close(); stream = None
                    queue_state = read(experiment/'queue-state.json', {})
                    if code or queue_state.get('state') == 'paused':
                        if lock_alive(experiment/'queue.lock') is True:
                            continue
                        update('needs_attention', queue_state.get('reason') or f'队列退出码{code}；请查看后台日志。')
                        return
                owner = lock_alive(experiment/'queue.lock')
                if owner is not False:
                    update('following', '当前模型一直在运行；待现有队列结束后自动顺接，不重复启动GPU任务。')
                    time.sleep(poll_seconds)
                    continue
                snapshot = progress(experiment)
                if snapshot['state'] == 'training_completed':
                    update('completed', '全部登记识别训练完成；正式测试仍需配置封存，不自动宣布模型可发布。')
                    return
                if snapshot['state'] == 'needs_attention':
                    update('needs_attention', snapshot['next_step'])
                    return
                first = read(experiment/'runs/FL-F2-s17/status.json', {})
                if first.get('state') == 'completed':
                    gate = m2_gate(experiment)
                    if not gate['passed']:
                        update('needs_attention', 'M2工程门槛未通过：'+','.join(k for k, passed in gate['checks'].items() if not passed))
                        atomic_json(experiment/'gates/M2.json', gate)
                        return
                    if not (experiment/'gates/M2.json').exists():
                        atomic_json(experiment/'gates/M2.json', gate)
                        append(experiment, 'transition', 'M2工程检查通过，进入后续识别实验',
                               '完整训练、学习信号、精确恢复和资源门槛通过；精度达标与业务发布另行验收。')
                args = [sys.executable, '-u', '-m', 'mdva.training', 'queue', '--experiment', str(experiment)]
                if first.get('state') != 'completed':
                    args.extend(['--max-runs', '1'])
                update('starting', '即将从尚未完成的实验继续，保持数据、配置和检查点身份核验。')
                stream = (experiment/'continuation-console.log').open('a', encoding='utf-8')
                child = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=stream,
                                         stderr=subprocess.STDOUT, cwd=Path(__file__).resolve().parents[3],
                                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except Exception as exc:
            update('needs_attention', str(exc))
            raise
        finally:
            if stream:
                stream.close()


if __name__ == '__main__':
    from .runtime import configure_environment
    from .__main__ import DEFAULT_EXPERIMENT
    configure_environment()
    run(DEFAULT_EXPERIMENT)
