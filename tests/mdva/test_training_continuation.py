import json

from mdva.training import continuation
from mdva.training.journal import append, entries, progress
from mdva.training.runtime import atomic_json


def experiment(tmp_path, completed=False):
    folder = tmp_path/'experiment'; folder.mkdir()
    configs = [{'id': 'FL-F2-s17', 'min_epochs': 20}, {'id': 'AR-A1-s17'}]
    atomic_json(folder/'protocol.json', {'recognition': configs})
    atomic_json(folder/'queue.json', {'order': [c['id'] for c in configs]})
    if completed:
        run = folder/'runs/FL-F2-s17'; run.mkdir(parents=True)
        (run/'last.pt').write_bytes(b'local checkpoint')
        (run/'best.pt').write_bytes(b'local best')
        atomic_json(run/'status.json', {'id': 'FL-F2-s17', 'state': 'completed', 'formal': True,
                    'completed_epoch': 20, 'best_epoch': 4, 'best_validation_dice': .5,
                    'resource': {'peak_reserved_gib': 2., 'peak_rss_gib': 2.5, 'peak_total_vram_used_gib': 4.}})
        atomic_json(run/'metrics.json', [{'epoch': n, 'train_loss': 1./n,
                    'validation': {'dice': .5, 'loss': .3}} for n in range(1,21)])
        atomic_json(folder/'diagnostics/resume-verification.json', {'passed': True, 'max_weight_difference': 0.})
    return folder


def test_m2_checks_real_completion_finite_history_resources_and_minimum_epochs(tmp_path):
    folder = experiment(tmp_path, completed=True)
    assert continuation.m2_gate(folder)['passed']
    file = folder/'runs/FL-F2-s17/status.json'
    state = json.loads(file.read_text()); state['resource']['peak_reserved_gib'] = 7
    atomic_json(file, state)
    assert not continuation.m2_gate(folder)['checks']['resource_limits']
    state['completed_epoch'] = 19; atomic_json(file, state)
    assert not continuation.m2_gate(folder)['checks']['minimum_training_epochs']


def test_following_live_external_queue_never_starts_second_gpu_job(tmp_path, monkeypatch):
    folder = experiment(tmp_path)
    calls = []
    def lock_alive(path):
        return not (folder/'PAUSE').exists()
    monkeypatch.setattr(continuation, 'lock_alive', lock_alive)
    monkeypatch.setattr(continuation.time, 'sleep', lambda _: (folder/'PAUSE').touch())
    monkeypatch.setattr(continuation.subprocess, 'Popen', lambda *a, **k: calls.append(a))
    continuation.run(folder, poll_seconds=0)
    assert calls == []
    assert json.loads((folder/'continuation.json').read_text(encoding='utf-8'))['state'] == 'paused'
    assert (folder/'训练进度与接续.md').exists()


def test_completed_m2_automatically_launches_remaining_queue_without_one_run_limit(tmp_path, monkeypatch):
    folder = experiment(tmp_path, completed=True)
    calls = []
    class Child:
        pid = 123
        returncode = None
        def poll(self): return self.returncode
    child = Child()
    def start(args, **kwargs):
        calls.append(args); return child
    def stop(_):
        (folder/'PAUSE').touch(); child.returncode = 0
    monkeypatch.setattr(continuation, 'lock_alive', lambda p: False)
    monkeypatch.setattr(continuation.time, 'sleep', stop)
    monkeypatch.setattr(continuation.subprocess, 'Popen', start)
    continuation.run(folder, poll_seconds=0)
    assert len(calls) == 1
    assert 'queue' in calls[0] and '--max-runs' not in calls[0]
    assert json.loads((folder/'gates/M2.json').read_text(encoding='utf-8'))['passed']
    assert any('M2工程检查通过' in r['title'] for r in entries(folder, page_size=50)['entries'])


def test_journal_is_persistent_paginated_and_logs_epoch_only_once(tmp_path):
    folder = experiment(tmp_path, completed=True)
    continuation.observe(folder)
    count = entries(folder)['total']
    continuation.observe(folder)
    assert entries(folder)['total'] == count
    append(folder, 'development', '开发记录', '下一步命令及交接文件已经保存。')
    assert entries(folder, kind='development')['total'] == 1
    assert len(entries(folder, page_size=1)['entries']) == 1
    assert progress(folder)['completed_runs'] == 1
