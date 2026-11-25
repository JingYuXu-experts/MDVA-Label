import json

import pytest

from mdva.training import watchdog
from mdva.training.runtime import atomic_json


def setup(tmp_path, state='running', error=''):
    folder = tmp_path/'experiment'
    atomic_json(folder/'protocol.json', {'recognition': [{'id': 'FL-F0-s17'}, {'id': 'AR-A1-s17'}]})
    atomic_json(folder/'queue.json', {'order': ['AR-A1-s17', 'FL-F0-s17']})
    atomic_json(folder/'runs/AR-A1-s17/status.json', {'id': 'AR-A1-s17', 'state': state, 'error': error})
    return folder


FREE = {'continuation': False, 'queue': False, 'gpu': False}


def test_recovery_follows_frozen_queue_not_protocol_order(tmp_path):
    folder = setup(tmp_path)
    assert watchdog.decision(folder, FREE, {})[:2] == ('recover', 'AR-A1-s17')
    atomic_json(folder/'queue.json', {'order': ['not-registered']})
    assert watchdog.decision(folder, FREE, {})[0] == 'blocked'


@pytest.mark.parametrize('marker', ['PAUSE', 'runs/AR-A1-s17/PAUSE'])
def test_explicit_pause_always_wins_even_with_live_workers(tmp_path, marker):
    folder = setup(tmp_path)
    (folder/marker).touch()
    assert watchdog.decision(folder, {**FREE, 'continuation': True}, {})[0] == 'paused'


def test_uncertain_or_owned_locks_never_spawn_second_trainer(tmp_path):
    folder = setup(tmp_path)
    assert watchdog.decision(folder, {**FREE, 'gpu': None}, {})[0] == 'blocked'
    assert watchdog.decision(folder, {**FREE, 'gpu': True}, {})[0] == 'following'
    assert watchdog.decision(folder, {**FREE, 'continuation': True}, {})[0] == 'following'
    assert watchdog.decision(folder, {**FREE, 'queue': True}, {})[0] == 'supervise'


@pytest.mark.parametrize('message', ['CUDA out of memory', '内存门槛不满足', '显存门槛不满足'])
def test_known_transient_failures_allow_bounded_recovery(tmp_path, message):
    folder = setup(tmp_path, 'resource_failed', message)
    assert watchdog.decision(folder, FREE, {})[0] == 'recover'
    assert watchdog.decision(folder, FREE, {'AR-A1-s17': 2})[0] == 'blocked'
    assert watchdog.decision(folder, FREE, {'previous': 6})[0] == 'blocked'


@pytest.mark.parametrize('message', ['non-finite logits', '配置、代码或数据版本不同',
                                   'out of memory with NaN', 'Permission denied', 'file missing'])
def test_unknown_or_scientific_failure_is_not_hidden_by_retry(tmp_path, message):
    folder = setup(tmp_path, 'failed', message)
    assert watchdog.decision(folder, FREE, {})[0] == 'blocked'


def test_watchdog_waits_for_resources_then_preserves_evidence_and_resumes(tmp_path, monkeypatch):
    folder = setup(tmp_path, 'resource_failed', 'CUDA out of memory')
    launched, sleeps = [], []
    monkeypatch.setattr(watchdog, 'lock_alive', lambda p: False)
    monkeypatch.setattr(watchdog, 'keep_awake', lambda enabled: True)
    ready = iter([(False, 'resources busy'), (True, 'ready')])
    monkeypatch.setattr(watchdog, 'resource_readiness', lambda p: next(ready))
    class Result:
        returncode = 0
        stdout = json.dumps({'checkpoint_epoch': 16, 'identity_matches': True})
    monkeypatch.setattr(watchdog.subprocess, 'run', lambda *a, **k: Result())
    def launch(p, command):
        launched.append(command)
        (p/'PAUSE').touch()
    monkeypatch.setattr(watchdog, 'launch', launch)
    monkeypatch.setattr(watchdog.time, 'sleep', lambda seconds: sleeps.append(seconds))
    watchdog.run(folder, poll_seconds=0)
    assert launched == ['queue']
    assert json.loads((folder/'watchdog-retries.json').read_text(encoding='utf-8'))['attempts'] == {'AR-A1-s17': 1}
    evidence = json.loads(next((folder/'recovery').glob('*.json')).read_text(encoding='utf-8'))
    assert evidence['state_before']['state'] == 'resource_failed'
    assert evidence['verification']['checkpoint_epoch'] == 16
    # Original failure status remains intact until the actual trainer updates it.
    assert json.loads((folder/'runs/AR-A1-s17/status.json').read_text(encoding='utf-8'))['state'] == 'resource_failed'
    assert json.loads((folder/'watchdog.json').read_text(encoding='utf-8'))['state'] == 'paused'


def test_failed_checkpoint_validation_blocks_launch(tmp_path, monkeypatch):
    folder = setup(tmp_path)
    monkeypatch.setattr(watchdog, 'lock_alive', lambda p: False)
    monkeypatch.setattr(watchdog, 'keep_awake', lambda enabled: True)
    monkeypatch.setattr(watchdog, 'resource_readiness', lambda p: (True, 'ready'))
    class Result:
        returncode = 1
        stderr = 'checkpoint identity mismatch'
    monkeypatch.setattr(watchdog.subprocess, 'run', lambda *a, **k: Result())
    monkeypatch.setattr(watchdog, 'launch', lambda *args: pytest.fail('must not launch'))
    watchdog.run(folder, poll_seconds=0)
    assert json.loads((folder/'watchdog.json').read_text(encoding='utf-8'))['state'] == 'blocked'
    assert not (folder/'watchdog-retries.json').exists()


def test_user_pause_during_verification_cancels_restart(tmp_path, monkeypatch):
    folder = setup(tmp_path)
    monkeypatch.setattr(watchdog, 'lock_alive', lambda p: False)
    monkeypatch.setattr(watchdog, 'keep_awake', lambda enabled: True)
    monkeypatch.setattr(watchdog, 'resource_readiness', lambda p: (True, 'ready'))
    class Result:
        returncode = 0
        stdout = '{}'
    def verify(*args, **kwargs):
        (folder/'PAUSE').touch()
        return Result()
    monkeypatch.setattr(watchdog.subprocess, 'run', verify)
    monkeypatch.setattr(watchdog, 'launch', lambda *args: pytest.fail('pause must win'))
    watchdog.run(folder, poll_seconds=0)
    assert json.loads((folder/'watchdog.json').read_text(encoding='utf-8'))['state'] == 'paused'
