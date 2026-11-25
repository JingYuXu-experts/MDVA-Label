"""Explicit per-family numerical revisions; previous attempts remain immutable."""
from pathlib import Path
import json
import re


def location(experiment, identifier):
    experiment = Path(experiment).resolve()
    registry = experiment/'run-revisions.json'
    mapping = json.loads(registry.read_text(encoding='utf-8')).get('runs', {}) if registry.exists() else {}
    entry = mapping.get(identifier)
    if not entry:
        return experiment, 'mdva.training.train'
    relative = entry.get('relative', '')
    reviewed = (re.fullmatch(r'(AR-A[01]|CH-C[12])-s(17|42|2026)', identifier)
                and relative == 'revisions/io-reviewed-v1'
                and entry.get('entrypoint') == 'mdva.training.reviewed_train')
    fast = (re.fullmatch(r'(AR-A[01]|CH-C[12])-s(17|42|2026)', identifier)
            and relative == 'revisions/io-fast-v1'
            and entry.get('entrypoint') == 'mdva.training.fast_train')
    original_ch = (re.fullmatch(r'CH-C[012]-s(17|42|2026)', identifier)
                   and re.fullmatch(r'revisions/ch-fp32-v\d+', relative)
                   and entry.get('entrypoint') == 'mdva.training.cnn193_fp32')
    guarded = (identifier in ('AR-A1-s2026', 'AR-A0-s42', 'AR-A0-s2026')
               and relative == 'revisions/io-ar-guard-v1'
               and entry.get('entrypoint') == 'mdva.training.ar_guard_train')
    if not (reviewed or original_ch or fast or guarded):
        raise ValueError('未登记的模型数值修订路径')
    if guarded:
        from .runtime import digest
        gate = json.loads((experiment/'ar-precision-revision.json').read_text(encoding='utf-8'))
        if not gate.get('enabled') or not gate.get('acceptance_passed') or identifier not in gate.get('runs', {}):
            raise ValueError('AR精度修订未通过验收或不在获准范围')
        for name in ('fast_data.py', 'ar_precision.py', 'ar_guard_train.py'):
            if gate.get('code', {}).get(name) != digest(Path(__file__).parent/name):
                raise ValueError('AR精度代码身份变化，禁止恢复')
    if fast:
        from .runtime import digest
        gate=json.loads((experiment/'throughput-revision.json').read_text(encoding='utf-8'))
        if not gate.get('enabled') or identifier not in gate.get('runs',{}):
            raise ValueError('效率修订未通过验收或实验不在获准范围')
        for name in ('fast_data.py','fast_train.py'):
            if gate.get('code',{}).get(name)!=digest(Path(__file__).parent/name):
                raise ValueError('效率代码版本改变，禁止沿用已验收记录')
    folder = (experiment/relative).resolve()
    if not folder.is_relative_to(experiment):
        raise ValueError('模型修订越出实验目录')
    return folder, entry['entrypoint']


def run_folder(experiment, identifier):
    return location(experiment, identifier)[0]/'runs'/identifier


def model_and_identity(config):
    from .runtime import code_identity
    if config.get('execution_revision') == 'io-ar-guard-v1':
        from .ar_precision import architecture
        from .ar_guard_train import code_identity
        return architecture, code_identity
    if config.get('execution_revision') == 'io-fast-v1':
        from .fast_train import architecture,code_identity
        return architecture,code_identity
    if config.get('execution_revision') == 'io-reviewed-v1':
        from .reviewed_train import architecture, code_identity
        return architecture, code_identity
    if config.get('cnn193_compute') == 'fp32':
        from .cnn193_fp32 import architecture, identity
        return architecture, identity
    from .stable_models import architecture
    return architecture, code_identity
