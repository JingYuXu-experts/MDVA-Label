"""Numerical adapter for author Flat U-Net, preserving its parameters and equations.

Convolutions still use AMP. Attention matmul/reductions/softmax/residual BN use
float32: half-precision spatial sums can overflow even with finite weights.
This is a numerical adaptation, not a new attention architecture.
"""
from __future__ import annotations

import types

import torch

from .models import architecture as original_architecture
from .runtime import digest


def stable_attention(self, inputs):
    fq, fk, fv = self.fq(inputs), self.fk(inputs), self.fv(inputs)
    with torch.autocast(inputs.device.type, enabled=False):
        fq, fk, fv = fq.float(), fk.float(), fv.float()
        if type(self).__name__ == 'CSA_ConvBlock':
            height = inputs.shape[2]
            fq = fq.unsqueeze(2)
            fk = fk.unsqueeze(1).permute(0, 1, 2, 4, 3)
            similarity = torch.matmul(fq, fk)/(fq.shape[-1]**.5)
            total = similarity.sum(dim=2)
            scores = total.sum(dim=(-2, -1))/(height**2)
        else:
            similarity = torch.matmul(fq, fk.transpose(2, 3))/(fq.shape[-1]**.5)
            scores = similarity.sum(dim=(2, 3))
        weights = torch.softmax(scores, dim=1).unsqueeze(-1).unsqueeze(-1)
        return self.relu(self.bn(weights*fv + inputs.float()))


def stabilize_attention(model):
    count = 0
    for layer in model.modules():
        if type(layer).__name__ in ('SCA_ConvBlock', 'CSA_ConvBlock'):
            layer.forward = types.MethodType(stable_attention, layer)
            count += 1
    return count


def architecture(config):
    model, provenance = original_architecture(config)
    if config['model'] == 'flat_unet':
        torch.set_float32_matmul_precision('highest')
        count = stabilize_attention(model)
        provenance = {**provenance, 'numerical_adaptation': {
            'policy': 'AMP convolutions; float32 attention matmul, reductions, softmax and residual BN',
            'blocks': count, 'adapter_sha256': digest(__file__),
            'float32_matmul_precision': 'highest',
            'equations_and_parameter_shapes_changed': False,
            'reason': 'first full run exposed finite-weights FP16 nonfinite validation'}}
    return model, provenance
