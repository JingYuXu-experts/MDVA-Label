"""Unattended recovery around the frozen trainer; never edits experiment inputs."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .continuation import lock_alive
from .journal import append, read
from .run_locations import run_folder, location
from .runtime import atomic_json, configure_environment, exclusive, now

ROOT = Path(__file__).resolve().parents[3]
MAX_PER_RUN = 2
MAX_TOTAL = 6


def decision(experiment, owners, attempts):
    """Pure scheduling decision. A pause or uncertain lock can never be bypassed."""
    experiment = Path(experiment)
    configs = read(experiment/'protocol.json')['recognition']
    registered = {c['id'] for c in configs}
    order = read(experiment/'queue.json', {}).get('order', [c['id'] for c in configs])
    if set(order) != registered or len(order) != len(registered):
        return 'blocked', None, '队列与登记实验不一致，不自行重排实验。'
    runs = [(identifier, run_folder(experiment, identifier)) for identifier in order]
    if (experiment/'PAUSE').exists() or any((p/'PAUSE').exists() for _, p in runs):
        return 'paused', None, '用户暂停标记仍在，守护不解除暂停。'
    if any(v is None for v in owners.values()):
        return 'blocked', None, '无法核验进程锁，不抢占GPU。'
    if owners['continuation']:
        return 'following', None, '队列正常运行，守护只监看，不启动第二个训练。'
    if owners['queue']:
        return 'supervise', None, '训练队列仍在，重新接入日志与队列监督。'
    if owners['gpu']:
        return 'following', None, 'GPU锁仍由现有任务持有，等待其退出。'
    states = [(identifier, read(path/'status.json', {})) for identifier, path in runs]
    pending = [(i, s) for i, s in states if s.get('state') != 'completed']
    if not pending:
        return 'completed', None, '全部登记训练结束，不自动解锁测试集。'
    identifier, state = pending[0]
    # Only transient failures have automatic remedies. NaNs, data errors and
    # identity mismatches require an investigated numerical/data revision.
    error = state.get('error', '').lower()
    numerical = ('nan', 'non-finite', '非有限', '版本不同', '身份', '损坏', 'size mismatch')
    transient = ('out of memory', '内存门槛', '显存门槛', 'driver shutting down',
                 'cuda-capable device', 'connection timed out', 'winerror 1455')
    recoverable = state.get('state') == 'running' or (
        state.get('state') in ('failed', 'resource_failed') and
        any(t in error for t in transient) and not any(t in error for t in numerical))
    if state.get('state') in ('failed', 'resource_failed') and not recoverable:
        return 'blocked', identifier, '异常需要审计，保留失败证据，不自动改模型或训练配方：'+state.get('error', '')
    if state.get('state') == 'paused':
        return 'paused', identifier, '实验已暂停，守护不擅自重启。'
    if attempts.get(identifier, 0) >= MAX_PER_RUN or sum(attempts.values()) >= MAX_TOTAL:
        return 'blocked', identifier, '已达到有限重试次数，保留检查点与故障记录。'
    return ('recover' if recoverable else 'start'), identifier, '核验冻结配置和检查点后恢复，最多重算未保存的一轮。'


def resource_readiness(experiment):
    import psutil
    if psutil.virtual_memory().available < 8 * 2**30:
        return False, '等待可用内存恢复到8GiB'
    if min(shutil.disk_usage(experiment).free, shutil.disk_usage(sys.prefix).free) < 8 * 2**30:
        return False, '等待输出盘与环境盘至少各有8GiB空间'
    try:
        result = subprocess.run(['nvidia-smi', '--query-gpu=memory.free', '--format=csv,noheader,nounits'],
                                capture_output=True, text=True, timeout=15,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        free = float(result.stdout.strip().splitlines()[0])
        if result.returncode or free < 6144:
            return False, '等待至少6GiB空闲显存后重试'
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return False, '等待GPU驱动恢复可查询状态'
    return True, '资源满足恢复门槛'


def verify(experiment, identifier):
    """Run in a short CPU-only process so the watchdog retains no tensor memory."""
    import torch
    from .train import data_identity
    from .runtime import code_identity
    from .cnn193_fp32 import identity as ch_identity
    from .continuation import m2_gate
    experiment = Path(experiment)
    if not m2_gate(experiment)['passed']:
        raise ValueError('M2工程门槛未通过，不能自动继续后续实验')
    scope, _ = location(experiment, identifier)
    protocol = read(scope/'protocol.json')
    config = read(scope/'configs'/f'{identifier}.json')
    registered = next(c for c in protocol['recognition'] if c['id'] == identifier)
    if config != registered:
        raise ValueError('配置与冻结协议不同')
    reference = read(experiment/'runs/FL-F2-s17/identity.json')
    if reference['code'] != code_identity():
        raise ValueError('训练源码与冻结身份不同')
    data = data_identity(Path(protocol['dataset']))
    if data != reference['data']:
        raise ValueError('数据清单与冻结身份不同')
    folder = run_folder(experiment, identifier)
    identity = read(folder/'identity.json')
    if identity:
        expected = {'config': config, 'data': data,
                    'code': ch_identity() if config.get('cnn193_compute') == 'fp32' else code_identity(),
                    'device': 'cuda', 'diagnostic_limit': None}
        if identity != expected:
            raise ValueError('运行身份与当前实验不同')
        saved = torch.load(folder/'last.pt', map_location='cpu', weights_only=False)
        required = {'model', 'optimizer', 'scheduler', 'scaler', 'rng', 'history', 'identity', 'epoch'}
        if not required <= saved.keys() or saved['identity'] != expected or len(saved['history']) != saved['epoch']:
            raise ValueError('检查点结构或身份不完整')
        if not all(torch.isfinite(v).all() for v in saved['model'].values() if torch.is_floating_point(v)):
            raise ValueError('检查点模型包含非有限数值')
        return {'id': identifier, 'checkpoint_epoch': saved['epoch'], 'identity_matches': True}
    if (folder/'last.pt').exists():
        raise ValueError('存在无法核验身份的检查点')
    return {'id': identifier, 'checkpoint_epoch': None, 'new_run': True, 'identity_matches': True}


def launch(experiment, command):
    with (experiment/'watchdog-console.log').open('ab') as stream:
        return subprocess.Popen([sys.executable, '-u', '-m', 'mdva.training', command,
                                 '--experiment', str(experiment)], cwd=ROOT,
                                stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).pid


def keep_awake(enabled):
    # Thread-scoped: Windows releases the request when this process exits.
    # Keep the system running but allow the screen to turn off normally.
    if os.name == 'nt':
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (1 if enabled else 0)))
    return False


def run(experiment, poll_seconds=15):
    experiment = Path(experiment).resolve()
    ledger = read(experiment/'watchdog-retries.json', {'attempts': {}, 'last_attempt': 0})
    attempts = ledger['attempts']
    previous = None
    waiting_since = None
    with exclusive(experiment/'watchdog.lock'):
        awake = keep_awake(True)
        def update(state, reason, identifier=None):
            nonlocal previous
            atomic_json(experiment/'watchdog.json', {'state': state, 'reason': reason,
                        'updated_utc': now(), 'pid': os.getpid(), 'run_id': identifier,
                        'system_sleep_inhibited': awake, 'attempts': attempts,
                        'max_per_run': MAX_PER_RUN, 'max_total': MAX_TOTAL})
            key = (state, identifier)
            if key != previous:
                label = {'following': '正在监看', 'supervise': '恢复队列监督', 'paused': '遵从暂停',
                         'completed': '队列已完成', 'blocked': '已保留故障待排查',
                         'waiting_resources': '等待资源恢复', 'recovering': '正在自动恢复'}
                append(experiment, 'error' if state == 'blocked' else 'handoff',
                       '夜间守护：'+label.get(state, state), reason, run_id=identifier,
                       details={'system_sleep_inhibited': awake, 'attempts': dict(attempts)})
                previous = key
        try:
            while True:
                owners = {'continuation': lock_alive(experiment/'continuation.lock'),
                          'queue': lock_alive(experiment/'queue.lock'),
                          'gpu': lock_alive(ROOT/'.mdva/training-gpu.lock')}
                action, identifier, reason = decision(experiment, owners, attempts)
                if action not in ('recover', 'start'):
                    update(action, reason, identifier)
                if action in ('paused', 'completed', 'blocked'):
                    return
                if action == 'supervise':
                    launch(experiment, 'continue')
                elif action in ('recover', 'start'):
                    ready, resource_reason = resource_readiness(experiment)
                    if not ready:
                        waiting_since = waiting_since or time.monotonic()
                        update('waiting_resources', resource_reason, identifier)
                        if time.monotonic()-waiting_since > 1800:
                            update('blocked', '资源30分钟未恢复；保存记录，不无限重试。', identifier)
                            return
                    elif time.time()-ledger['last_attempt'] >= 60:
                        waiting_since = None
                        # Recheck locks and pause after waiting; a user may have resumed/stopped elsewhere.
                        current_owners = {'continuation': lock_alive(experiment/'continuation.lock'),
                                          'queue': lock_alive(experiment/'queue.lock'),
                                          'gpu': lock_alive(ROOT/'.mdva/training-gpu.lock')}
                        current = decision(experiment, current_owners, attempts)
                        if current[:2] != (action, identifier):
                            continue
                        result = subprocess.run([sys.executable, '-m', 'mdva.training.watchdog',
                                   '--experiment', str(experiment), '--verify', identifier],
                                   capture_output=True, text=True, encoding='utf-8', timeout=120,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                        if result.returncode:
                            update('blocked', '恢复前检查失败：'+result.stderr[-4000:], identifier)
                            return
                        folder = run_folder(experiment, identifier)
                        attempts[identifier] = attempts.get(identifier, 0)+1
                        stamp = time.time_ns()
                        evidence = experiment/'recovery'/f'{identifier}-{stamp}.json'
                        atomic_json(evidence, {'time': now(), 'state_before': read(folder/'status.json'),
                                    'queue_before': read(experiment/'queue-state.json'),
                                    'verification': json.loads(result.stdout),
                                    'attempt': attempts[identifier], 'action': action})
                        ledger['last_attempt'] = time.time()
                        atomic_json(experiment/'watchdog-retries.json', ledger)
                        # Trainer takes the global GPU lock and verifies identity again.
                        if (experiment/'PAUSE').exists() or (folder/'PAUSE').exists():
                            continue
                        launch(experiment, 'queue')
                        update('recovering', '原配置检查通过，从完整检查点接续；故障证据：'+str(evidence), identifier)
                time.sleep(poll_seconds)
        except Exception as exc:
            update('blocked', '守护本身遇到异常，未改动正在运行的训练：'+str(exc))
            raise
        finally:
            keep_awake(False)
            final_state = read(experiment/'watchdog.json', {})
            final_state['system_sleep_inhibited'] = False
            final_state['exited_utc'] = now()
            atomic_json(experiment/'watchdog.json', final_state)


if __name__ == '__main__':
    import argparse
    from .__main__ import DEFAULT_EXPERIMENT
    parser = argparse.ArgumentParser()
    parser.add_argument('--experiment', type=Path, default=DEFAULT_EXPERIMENT)
    parser.add_argument('--verify')
    args = parser.parse_args()
    configure_environment()
    if args.verify:
        print(json.dumps(verify(args.experiment, args.verify)))
    else:
        run(args.experiment)
