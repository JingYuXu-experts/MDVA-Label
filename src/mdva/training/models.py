"""Explicit solar baselines. CNN193 is an independent modern reconstruction."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import torch
from torch import nn

from .runtime import digest


class ConvBlock(nn.Module):
    def __init__(self, inputs, outputs, cnn193=False):
        super().__init__()
        activation = nn.ELU if cnn193 else nn.ReLU
        layers = [nn.Conv2d(inputs, outputs, 3, padding=1), activation()]
        if cnn193:
            layers.append(nn.Dropout(.1))
        layers.extend([nn.Conv2d(outputs, outputs, 3, padding=1), activation()])
        self.layers = nn.Sequential(*layers)

    def forward(self, x):
        return self.layers(x)


class UNet(nn.Module):
    def __init__(self, width=32, cnn193=False):
        super().__init__()
        channels = [width * 2**i for i in range(5)]
        self.encoders = nn.ModuleList([ConvBlock(1 if i == 0 else channels[i-1], c, cnn193)
                                      for i, c in enumerate(channels)])
        self.ups = nn.ModuleList([nn.ConvTranspose2d(channels[i+1], channels[i], 2, 2)
                                 for i in reversed(range(4))])
        self.decoders = nn.ModuleList([ConvBlock(channels[i]*2, channels[i], cnn193)
                                      for i in reversed(range(4))])
        self.out = nn.Conv2d(width, 1, 1)

    def forward(self, x):
        skips = []
        for i, block in enumerate(self.encoders):
            if i:
                x = nn.functional.max_pool2d(x, 2)
            x = block(x)
            skips.append(x)
        for up, block, skip in zip(self.ups, self.decoders, reversed(skips[:-1])):
            x = block(torch.cat([up(x), skip], dim=1))
        return self.out(x)


def architecture(config):
    kind = config['model']
    if kind == 'unet':
        return UNet(config.get('width', 32)), {'implementation': 'MDVA U-Net adaptation'}
    if kind == 'cnn193':
        return UNet(24, cnn193=True), {'implementation': 'independent PyTorch reconstruction',
                'paper': 'https://arxiv.org/abs/1809.05748', 'depth': 4, 'initial_filters': 24}
    if kind != 'flat_unet':
        raise ValueError('未知分割架构')
    folder = Path(sys.prefix) / 'model_sources/flat_unet'
    source = folder / 'fs_model.py'
    provenance = json.loads((folder / 'PROVENANCE.json').read_text(encoding='utf-8'))
    if digest(source) != provenance['model_source_sha256']:
        raise ValueError('Flat U-Net 作者源码哈希发生变化')
    spec = importlib.util.spec_from_file_location('mdva_author_flat', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    attention = [True] * 5 if config.get('attention') == 'all_sca' else [True]*4 + [False]
    return module.Flat_Unet(attention, flat_channels=config.get('width', 32)), provenance


class SequenceClassifier(nn.Module):
    def __init__(self, kind='lstm', steps=3, features=5):
        super().__init__()
        self.kind = kind
        if kind == 'lstm':
            self.encoder = nn.LSTM(features, 32, batch_first=True)
            self.head = nn.Linear(32, 1)
        elif kind == 'mlp':
            self.encoder = nn.Sequential(nn.Flatten(), nn.Linear(steps*features, 32), nn.ReLU())
            self.head = nn.Linear(32, 1)
        else:
            raise ValueError('未知时序分类器')

    def forward(self, x):
        x = self.encoder(x)[0][:, -1] if self.kind == 'lstm' else self.encoder(x)
        return self.head(x).squeeze(-1)
