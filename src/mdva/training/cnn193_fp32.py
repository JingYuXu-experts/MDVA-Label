"""CNN193 numerical repair: same architecture/loss, full FP32 forward graph."""
from pathlib import Path
import argparse
import json
import types

from .runtime import code_identity, digest


def identity():
    return {**code_identity(), 'cnn193_fp32.py': digest(__file__)}


def architecture(config):
    import torch
    from .stable_models import architecture as baseline
    if config['model'] != 'cnn193' or config.get('cnn193_compute') != 'fp32':
        raise ValueError('FP32修复入口仅用于登记的CNN193配置')
    model, provenance = baseline(config)
    torch.backends.cudnn.allow_tf32 = False
    original = model.forward
    def forward(self, image):
        with torch.autocast(image.device.type, enabled=False):
            return original(image.float())
    model.forward = types.MethodType(forward, model)
    return model, {**provenance, 'numerical_adaptation': {
        'precision': 'full FP32 forward; TF32 convolution disabled',
        'reason': 'reproduced finite-weight FP16 decoder convolution overflow',
        'equations_and_parameter_shapes_changed': False, 'source_sha256': digest(__file__)}}


def main():
    from .runtime import configure_environment
    configure_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--dataset', required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--stop-after', type=int)
    args = parser.parse_args()
    from . import train as trainer
    trainer.architecture = architecture
    trainer.code_identity = identity
    config = json.loads(Path(args.config).read_text(encoding='utf-8'))
    print(json.dumps(trainer.train(config, args.dataset, args.run, args.resume,
                                 stop_after=args.stop_after), ensure_ascii=False), flush=True)


if __name__ == '__main__': main()
