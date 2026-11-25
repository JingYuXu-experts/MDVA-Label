"""Finite-checked AMP for the deterministic, stateless MDVA AR U-Net.

FP16 forward overflow is retried once in FP32, before loss/backward. Finite
AMP outputs are returned unchanged. No clipping, skipped samples, or NaN masks.
"""
from __future__ import annotations

import types

import torch

from .runtime import digest

POLICY = 'AMP; retry nonfinite forward once in FP32; reject nonfinite FP32'


def guard(model, on_retry=None):
    # Replaying a forward is only valid for this graph: no dropout, running
    # statistics, random draws or other stateful layers may be added silently.
    from .models import UNet
    allowed = (torch.nn.Conv2d, torch.nn.ConvTranspose2d, torch.nn.ReLU)
    if type(model) is not UNet or any(
        not isinstance(m, allowed) for m in model.modules() if not list(m.children())
    ) or list(model.buffers()):
        raise ValueError('精度回退仅允许无随机层、无可变缓冲的AR U-Net')
    original = model.forward
    counters = {'forwards': 0, 'fp32_retries': 0, 'consecutive_retries': 0}

    def forward(self, image):
        output = original(image)
        counters['forwards'] += 1
        if bool(torch.isfinite(output).all()):
            counters['consecutive_retries'] = 0
            return output
        # Do not consume stochastic state a second time or propagate the bad
        # graph into backward. Inputs and all parameters must still be finite.
        if not bool(torch.isfinite(image).all()) or not all(
            bool(torch.isfinite(p).all()) for p in self.parameters()
        ):
            raise RuntimeError('输入或参数非有限，禁止精度重试掩盖损坏')
        del output
        with torch.autocast(image.device.type, enabled=False):
            output = original(image.float())
        if not bool(torch.isfinite(output).all()):
            raise RuntimeError('FP32重试仍非有限，停止并保留上一完整epoch')
        counters['fp32_retries'] += 1
        counters['consecutive_retries'] += 1
        if counters['consecutive_retries'] > 128:
            raise RuntimeError('连续FP32重试超过128次，停止评估持续数值不稳定')
        if on_retry:
            on_retry({'forward': counters['forwards'], 'retry': counters['fp32_retries'],
                      'consecutive_retries': counters['consecutive_retries'],
                      'training': self.training, 'batch': len(image), 'policy': POLICY})
        return output

    model.forward = types.MethodType(forward, model)
    model.precision_counters = counters
    return model


def architecture(config, on_retry=None):
    from .stable_models import architecture as baseline
    if config.get('category') != 'AR' or config.get('model') != 'unet' or config.get('ar_precision_policy') != POLICY:
        raise ValueError('精度修复仅用于登记的AR U-Net配置')
    model, provenance = baseline(config)
    return guard(model, on_retry), {**provenance, 'numerical_adaptation': {
        'policy': POLICY, 'equations_and_parameter_shapes_changed': False,
        'finite_amp_outputs_unchanged': True, 'source_sha256': digest(__file__),
        'reason': 'reproduced epoch28 decoder FP16 overflow with finite inputs and weights'}}
