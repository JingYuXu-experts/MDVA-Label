"""Independent PyTorch translation of the author's pinned U-Net graph.

Reference: observethesun/helio@45dabe6aa0a287c876cf598589f2003d8049e15e,
helio/models/models.py. This is not TF numerical-parity certification or
reproduction of its original dataset/augmentation/optimizer implementation.
"""
from __future__ import annotations

import torch
from torch import nn


class Block(nn.Sequential):
    def __init__(self,inputs,outputs,dropout=False):
        layers=[nn.Conv2d(inputs,outputs,3,padding=1),nn.ELU(),
                nn.Conv2d(outputs,outputs,3,padding=1),nn.ELU()]
        if dropout:layers.append(nn.Dropout(.1))
        super().__init__(*layers)


class SameUp(nn.Module):
    def __init__(self,inputs,outputs):
        super().__init__()
        self.conv=nn.ConvTranspose2d(inputs,outputs,3,stride=2)
        self.activation=nn.ELU();self.dropout=nn.Dropout(.1)

    def forward(self,x):
        # TF SAME K=3,S=2 has total cropping 1: before=0, after=1.
        out=self.conv(x)[...,:x.shape[-2]*2,:x.shape[-1]*2]
        return self.dropout(self.activation(out))


class AuthorCNN193(nn.Module):
    def __init__(self,width=24):
        super().__init__()
        channels=[width*2**i for i in range(5)]
        self.encoders=nn.ModuleList([Block(1 if i==0 else channels[i-1],c) for i,c in enumerate(channels[:-1])])
        self.down=nn.Sequential(nn.MaxPool2d(2),nn.Dropout(.1))
        self.bottom=Block(channels[-2],channels[-1],True)
        self.ups=nn.ModuleList([SameUp(channels[i],channels[i]) for i in range(4,0,-1)])
        self.decoders=nn.ModuleList([Block(channels[i]+channels[i-1],channels[i-1],True) for i in range(4,0,-1)])
        # The notebook's output dict uses a 3x3 convolution, not 1x1.
        self.out=nn.Conv2d(width,1,3,padding=1)
        for module in self.modules():
            if isinstance(module,(nn.Conv2d,nn.ConvTranspose2d)):
                nn.init.xavier_uniform_(module.weight);nn.init.zeros_(module.bias)

    def forward(self,x):
        with torch.autocast(x.device.type,enabled=False):
            x=x.float();skips=[]
            for block in self.encoders:
                x=block(x);skips.append(x);x=self.down(x)
            x=self.bottom(x)
            for up,block,skip in zip(self.ups,self.decoders,reversed(skips)):
                x=block(torch.cat((up(x),skip),1))
            return self.out(x)


def architecture(config):
    if config['model']!='cnn193_author' or config['width']!=24:raise ValueError('只允许登记的作者结构对照')
    return AuthorCNN193(),{'implementation':'independent PyTorch translation of pinned author graph',
        'author_commit':'45dabe6aa0a287c876cf598589f2003d8049e15e',
        'source':'https://github.com/observethesun/helio/blob/45dabe6aa0a287c876cf598589f2003d8049e15e/helio/models/models.py',
        'paper':'https://arxiv.org/abs/1809.05748',
        'adaptations':['PyTorch; no TensorFlow numerical parity claim','512 input and local fixed data/recipe',
                       'FP32 forward','logits output for numerically stable BCE; sigmoid at evaluation',
                       'all dropout follows train/eval; author pooling helper omitted the is_training argument']}
