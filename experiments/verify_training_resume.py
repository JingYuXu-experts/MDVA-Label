"""Real two-image CUDA integration check; outputs are diagnostic, never test results."""
from pathlib import Path
import json

from mdva.training.runtime import atomic_json, configure_environment


def main():
    configure_environment()
    import torch
    from mdva.training.train import load_checkpoint, train
    root = Path(__file__).resolve().parents[1]
    from mdva.training.__main__ import DEFAULT_EXPERIMENT
    experiment = DEFAULT_EXPERIMENT
    config = json.loads((experiment/'configs/FL-F2-s17.json').read_text(encoding='utf-8'))
    config.update(id='diagnostic-resume', max_epochs=3, min_epochs=3, patience=12, effective_batch=2)
    dataset = root/'out/pretraining-20261112/dataset-ready'
    direct, resumed = experiment/'diagnostics/resume-direct', experiment/'diagnostics/resume-split'
    train(config, dataset, direct, diagnostic_limit=2)
    train(config, dataset, resumed, diagnostic_limit=2, stop_after=1)
    train(config, dataset, resumed, diagnostic_limit=2, resume=True)
    a, b = load_checkpoint(direct/'last.pt'), load_checkpoint(resumed/'last.pt')
    difference = max(float((a['model'][k]-b['model'][k]).abs().max()) for k in a['model'])
    assert difference == 0., f'resume mismatch {difference}'
    assert a['epoch'] == b['epoch'] == 3
    assert a['best_epoch'] == b['best_epoch']
    assert a['scheduler'] == b['scheduler']
    atomic_json(experiment/'diagnostics/resume-verification.json',
                {'passed': True, 'max_weight_difference': difference, 'complete_epochs': 3,
                 'scope': 'real two FL images, CUDA AMP, uninterrupted vs1+2epochs', 'formal_test': False})
    print('真实CUDA检查点恢复：逐张量一致')


if __name__ == '__main__':
    main()
