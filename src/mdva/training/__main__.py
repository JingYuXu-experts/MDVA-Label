"""Unified entry point; imports GPU libraries only for GPU commands."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from .runtime import atomic_json, configure_environment, exclusive, now

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA = ROOT/'out/pretraining-20261112/dataset-ready'
DEFAULT_EXPERIMENT = ROOT/'out/training-20261112-v2'


def status(experiment):
    from .run_locations import run_folder
    protocol = json.loads((experiment/'protocol.json').read_text(encoding='utf-8'))
    runs = []
    for config in protocol['recognition']:
        file = run_folder(experiment, config['id'])/'status.json'
        runs.append(json.loads(file.read_text(encoding='utf-8')) if file.exists()
                    else {'id': config['id'], 'state': 'not_started', 'completed_epoch': 0})
    from .context import active_forecast
    forecasting = active_forecast(experiment)
    approved = json.loads((experiment/'approved-revision.json').read_text(encoding='utf-8')) if (experiment/'approved-revision.json').exists() else None
    revised = json.loads((experiment/'reviewed-state.json').read_text(encoding='utf-8')) if (experiment/'reviewed-state.json').exists() else None
    completed_ids = {r['id'] for r in runs if r['state'] == 'completed'}
    from .recognition_release import scope_counts
    approved_counts=scope_counts(protocol,runs,approved)
    return {'updated_utc': now(), 'experiment': str(experiment), 'runs': runs,
            **approved_counts,
            'approved_revision': approved.get('revision') if approved else None,
            'reviewed_state': revised,
            'approved_remaining_order': [identifier for identifier in approved['order'] if identifier not in completed_ids] if approved else None,
            'complete': sum(r['state'] == 'completed' for r in runs), 'total': len(runs),
            'forecast_experiment': str(forecasting),
            'forecast_test_runs': len(list((forecasting/'forecast-test').glob('*/summary.json'))),
            'forecast_state': json.loads((forecasting/'revision-status.json').read_text(encoding='utf-8')) if (forecasting/'revision-status.json').exists() else 'running_or_not_started',
            'forecast_gate': json.loads((experiment/'label-audit.json').read_text(encoding='utf-8'))['gates']}


def queue(experiment, maximum=None):
    if (experiment/'approved-revision.json').exists():
        raise ValueError('已启用有阶段门槛的修订队列，请用 python -m mdva.training.reviewed_queue；禁止回到旧42项队列')
    from .run_locations import location
    with exclusive(experiment/'queue.lock'):
        order = json.loads((experiment/'queue.json').read_text(encoding='utf-8'))['order']
        protocol = json.loads((experiment/'protocol.json').read_text(encoding='utf-8'))
        count = 0
        for identifier in order:
            if (experiment/'PAUSE').exists() or (maximum is not None and count >= maximum):
                break
            scope, entrypoint = location(experiment, identifier)
            run = scope/'runs'/identifier
            state_file = run/'status.json'
            state = json.loads(state_file.read_text(encoding='utf-8')) if state_file.exists() else {}
            if state.get('state') == 'completed':
                continue
            execution_protocol = json.loads((scope/'protocol.json').read_text(encoding='utf-8'))
            registered = next(c for c in execution_protocol['recognition'] if c['id'] == identifier)
            current_config = json.loads((scope/'configs'/f'{identifier}.json').read_text(encoding='utf-8'))
            if current_config != registered:
                raise ValueError('配置与冻结协议不符，禁止沿用原实验编号')
            args = [sys.executable, '-u', '-m', entrypoint, '--dataset', execution_protocol['dataset'],
                    '--config', str(scope/'configs'/f'{identifier}.json'), '--run', str(run)]
            if (run/'identity.json').exists():
                args.append('--resume')
            atomic_json(experiment/'queue-state.json', {'state': 'running', 'active': identifier,
                                                       'updated_utc': now(), 'command': args})
            run.mkdir(parents=True, exist_ok=True)
            with (run/'console.log').open('a', encoding='utf-8') as log:
                result = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            current = json.loads(state_file.read_text(encoding='utf-8')) if state_file.exists() else {}
            atomic_json(experiment/'progress.json', status(experiment))
            if result.returncode or current.get('state') != 'completed':
                atomic_json(experiment/'queue-state.json', {'state': 'paused', 'active': identifier,
                           'updated_utc': now(), 'reason': current.get('error', '运行暂停或尚未完成'),
                           'returncode': result.returncode})
                return
            count += 1
        atomic_json(experiment/'queue-state.json', {'state': 'idle', 'updated_utc': now(),
                   'reason': '队列完成或本次任务预算/暂停标记到达', 'completed_this_session': count})
        atomic_json(experiment/'progress.json', status(experiment))


def main():
    parser = argparse.ArgumentParser(description='MDVA 可恢复本地模型实验')
    parser.add_argument('command', choices=['freeze', 'status', 'queue', 'continue', 'pause', 'unpause',
                                          'forecast', 'evaluate', 'lock-test', 'package', 'report', 'authorize-test'])
    parser.add_argument('--dataset', type=Path, default=DEFAULT_DATA)
    parser.add_argument('--experiment', type=Path, default=DEFAULT_EXPERIMENT)
    parser.add_argument('--max-runs', type=int)
    parser.add_argument('--run-id')
    parser.add_argument('--protocol-sha256')
    parser.add_argument('--split', choices=['validation', 'calibration', 'test'], default='validation')
    args = parser.parse_args()
    experiment = args.experiment.resolve()
    if args.command == 'status':
        print(json.dumps(status(experiment), ensure_ascii=False, indent=2))
        return
    configure_environment()
    if args.command == 'freeze':
        from .protocol import freeze
        print(json.dumps(freeze(args.dataset, experiment), ensure_ascii=False, indent=2))
    elif args.command == 'queue':
        queue(experiment, args.max_runs)
    elif args.command == 'continue':
        if (experiment/'approved-revision.json').exists():
            raise ValueError('请运行 python -m mdva.training.reviewed_queue；旧连续队列包含已暂缓实验')
        from .continuation import run, lock_alive
        if lock_alive(experiment/'continuation.lock') is True:
            print('连续队列已在后台运行，无需重复启动；可在本地模型实验页查看进度。')
            return
        run(experiment)
    elif args.command == 'pause':
        experiment.mkdir(parents=True, exist_ok=True)
        (experiment/'PAUSE').touch()
        state_file = experiment/'queue-state.json'
        if state_file.exists():
            active = json.loads(state_file.read_text(encoding='utf-8')).get('active')
            if active:
                from .run_locations import run_folder
                (run_folder(experiment, active)/'PAUSE').touch()
        print('已请求在当前epoch边界暂停；状态以status为准')
    elif args.command == 'unpause':
        from .run_locations import run_folder
        protocol = json.loads((experiment/'protocol.json').read_text(encoding='utf-8'))
        for path in [experiment/'PAUSE', *(run_folder(experiment,c['id'])/'PAUSE' for c in protocol['recognition'])]:
            path.unlink(missing_ok=True)
        print('已清除暂停标记；执行queue继续')
    elif args.command == 'forecast':
        from .forecast import run_baselines
        from .context import active_forecast
        branch = active_forecast(experiment)
        protocol = json.loads((branch/'protocol.json').read_text(encoding='utf-8'))
        run_baselines(Path(protocol['dataset']), branch)
    elif args.command == 'authorize-test':
        from .recognition_release import authorize
        if not args.protocol_sha256:
            raise ValueError('需明确提供已向用户展示并获单独确认的--protocol-sha256')
        authorize(experiment,args.protocol_sha256)
        print('已记录独立确认；测试尚未执行')
    elif args.command in ('evaluate', 'lock-test', 'package', 'report'):
        from .delivery import dispatch
        dispatch(args.command, experiment, args.run_id, args.split)


if __name__ == '__main__':
    main()
