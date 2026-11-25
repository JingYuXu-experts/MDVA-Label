"""Bootstrap must work before engine imports and remember only successful setup."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / 'launcher.py'
    spec = importlib.util.spec_from_file_location('mdva_bootstrap_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'STATE', tmp_path / '.mdva/launcher.json')
    monkeypatch.delenv('MDVA_PY', raising=False)
    return module


def test_first_use_has_setup_and_does_not_mark_finished(launcher, monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, 'setup_window', lambda error: calls.append(error) or 0)
    assert launcher.main() == 0
    assert calls == ['']
    assert not launcher.STATE.exists()


def test_saved_success_skips_setup_and_reuses_selected_python(launcher, monkeypatch):
    launcher.remember('chosen-python')
    calls = []
    monkeypatch.setattr(launcher, 'probe', lambda executable: {'ok': executable == 'chosen-python'})
    monkeypatch.setattr(launcher, 'start_service', lambda executable: calls.append(executable))
    monkeypatch.setattr(launcher.webbrowser, 'open', lambda url: calls.append(url))
    monkeypatch.setattr(launcher, 'setup_window', lambda _: pytest.fail('Unexpected setup window'))
    assert launcher.main() == 0
    assert calls == ['chosen-python', launcher.URL]


def test_broken_saved_environment_reopens_setup(launcher, monkeypatch):
    launcher.remember('missing-python')
    monkeypatch.setattr(launcher, 'probe', lambda _: {'ok': False})
    calls = []
    monkeypatch.setattr(launcher, 'setup_window', lambda error: calls.append(error) or 0)
    assert launcher.main() == 0
    assert '环境发生变化' in calls[0]


def test_failed_service_reopens_setup_without_overwriting_choice(launcher, monkeypatch):
    launcher.remember('chosen-python')
    monkeypatch.setattr(launcher, 'probe', lambda _: {'ok': True})
    def fail(_): raise RuntimeError('端口冲突')
    monkeypatch.setattr(launcher, 'start_service', fail)
    errors = []
    monkeypatch.setattr(launcher, 'setup_window', lambda error: errors.append(error) or 0)
    launcher.main()
    assert errors == ['端口冲突']
    assert launcher.read_state()['python'] == 'chosen-python'


def test_invalid_state_and_profile_are_not_setup_completion(launcher):
    launcher.STATE.parent.mkdir()
    for value in ('broken', '[]', json.dumps({'profile': -1, 'python': 'x'})):
        launcher.STATE.write_text(value, encoding='utf-8')
        assert launcher.read_state() == {}


def test_same_service_requires_identity_and_workspace(launcher, monkeypatch):
    info = {'app': 'mdva', 'revision': 'web-solar-ai-4', 'monitor_revision':'sw-2', 'root': str(launcher.ROOT/'data'),
            'out_dir': str(launcher.ROOT/'out')}
    assert launcher.same_service(info)
    assert not launcher.same_service({**info, 'root': str(launcher.ROOT/'other')})
    monkeypatch.setattr(launcher, 'health', lambda: info)
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *a, **kw: pytest.fail('Started duplicate service'))
    launcher.start_service('unused')


def test_probe_explains_missing_interpreter(launcher):
    result = launcher.probe(str(launcher.ROOT/'missing-python.exe'))
    assert result['ok'] is False and result['failures']
