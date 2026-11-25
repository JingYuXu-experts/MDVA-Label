"""Controlled adaptation of the MIT backbone used by DeepFilamentSegmentation.

Original source and full MIT notice are preserved with the pinned local snapshot.
The GPL solar training/data scripts are reference-only and are never imported.
The forward returns logits instead of sigmoid for stable BCEWithLogits.
"""
import importlib.util
import json
from pathlib import Path
import torch
from torch import nn
from .runtime import digest

ROOT=Path(__file__).resolve().parents[3]
SOURCE=ROOT/'out/solar-model-sources/deepfilament-20261118'


class AuthorGraph(nn.Module):
    def __init__(self,normalization):
        super().__init__()
        if normalization not in ('none','batch'):raise ValueError('未登记的暗条归一化配置')
        record=json.loads((SOURCE/'provenance.json').read_text(encoding='utf-8'))
        source=SOURCE/'backbone/unet.py'
        for n in ('backbone/unet.py','backbone/LICENSE'):
            if digest(SOURCE/n)!=record['files'][n]['sha256']:raise ValueError('作者骨干来源身份改变')
        spec=importlib.util.spec_from_file_location('mdva_pinned_filament_backbone',source)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.network=module.UNet(in_channels=1,out_channels=1,init_features=32)
        if normalization=='none':
            # Construct the same graph first: all convolution initializations match.
            for parent in list(self.network.modules()):
                for name,child in list(parent.named_children()):
                    if isinstance(child,nn.BatchNorm2d):setattr(parent,name,nn.Identity())

    def forward(self,x):
        if x.ndim!=4 or x.shape[1]!=1 or x.shape[-1]%16 or x.shape[-2]%16:raise ValueError('暗条输入必须单通道且网格为16倍数')
        m=self.network
        with torch.autocast(x.device.type,enabled=False):
            e1=m.encoder1(x.float());e2=m.encoder2(m.pool1(e1));e3=m.encoder3(m.pool2(e2));e4=m.encoder4(m.pool3(e3))
            bottom=m.bottleneck(m.pool4(e4))
            d4=m.decoder4(torch.cat((m.upconv4(bottom),e4),dim=1))
            d3=m.decoder3(torch.cat((m.upconv3(d4),e3),dim=1))
            d2=m.decoder2(torch.cat((m.upconv2(d3),e2),dim=1))
            d1=m.decoder1(torch.cat((m.upconv1(d2),e1),dim=1))
            return m.conv(d1)


def loss_value(logits,target,positive_weight):
    if positive_weight not in (1,10):raise ValueError('只允许登记的正类权重1或10')
    if not torch.isfinite(logits).all():raise ValueError('暗条输出非有限')
    bce=nn.functional.binary_cross_entropy_with_logits(logits.float(),target.float(),pos_weight=logits.new_tensor(float(positive_weight)))
    p,y=logits.float().sigmoid().flatten(1),target.float().flatten(1)
    dice=1-((2*(p*y).sum(1)+1)/(p.sum(1)+y.sum(1)+1)).mean()
    return .5*bce+.5*dice
