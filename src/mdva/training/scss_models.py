"""SCSS-Net author-structure adaptation and one explicit GroupNorm hypothesis.

Reference: harmi97/sun-segmentation@d70e09c0187376ab1ff983a6ab864e7d089635f5.
This is not a reproduction of the authors' data or a TF numerical-parity claim.
"""
from __future__ import annotations

import torch
from torch import nn
from torch.nn import functional as F


def normalization(channels,kind):
    if kind=='batch':return nn.BatchNorm2d(channels,eps=.001,momentum=.01)
    if kind=='group':return nn.GroupNorm(8,channels,eps=.001)
    raise ValueError('仅允许预登记BatchNorm或GroupNorm')


class Block(nn.Sequential):
    def __init__(self,inputs,outputs,kind,transpose=False):
        conv=nn.ConvTranspose2d if transpose else nn.Conv2d
        super().__init__(conv(inputs,outputs,3,padding=1),normalization(outputs,kind),nn.ReLU(),
                         conv(outputs,outputs,3,padding=1),normalization(outputs,kind),nn.ReLU())


class SCSSNet(nn.Module):
    def __init__(self,norm='batch',width=32,dropout=.5):
        super().__init__()
        if norm not in ('batch','group') or width!=32 or dropout!=.5:raise ValueError('未登记SCSS结构')
        channels=[width*2**i for i in range(5)]
        self.encoders=nn.ModuleList([Block(1 if i==0 else channels[i-1],c,norm) for i,c in enumerate(channels[:4])])
        self.bottom=Block(channels[3],channels[4],norm)
        self.decoders=nn.ModuleList([Block(channels[i]+channels[i-1],channels[i-1],norm,True) for i in range(4,0,-1)])
        self.drop=nn.Dropout(dropout)
        self.out=nn.Conv2d(width,1,1)
        for m in self.modules():
            if isinstance(m,(nn.Conv2d,nn.ConvTranspose2d)):
                nn.init.xavier_uniform_(m.weight);nn.init.zeros_(m.bias)

    def forward(self,x):
        if x.ndim!=4 or x.shape[1]!=1 or x.shape[-2]%16 or x.shape[-1]%16:raise ValueError('SCSS需要单通道、16倍数的图像网格')
        # Explicit FP32 for both arms; never silently compare different precision.
        with torch.autocast(x.device.type,enabled=False):
            x=x.float();skips=[]
            for i,block in enumerate(self.encoders):
                if i>=2:x=self.drop(x)
                x=block(x);skips.append(x);x=F.max_pool2d(x,2)
            x=self.bottom(self.drop(x))
            for i,(block,skip) in enumerate(zip(self.decoders,reversed(skips))):
                if i<2:x=self.drop(x)
                x=block(torch.cat((F.interpolate(x,scale_factor=2,mode='nearest'),skip),1))
            # Algebraic logit interface to BCEWithLogits; sigmoid only at evaluation.
            return self.out(x)


def architecture(config):
    if config.get('model')!='scss' or config.get('normalization') not in ('batch','group'):
        raise ValueError('未登记SCSS模型')
    model=SCSSNet(config['normalization'])
    return model,{'implementation':'independent PyTorch author-structure adaptation',
        'source':'https://github.com/harmi97/sun-segmentation/blob/d70e09c0187376ab1ff983a6ab864e7d089635f5/src/model_scss_net.py',
        'paper':'https://doi.org/10.1093/mnras/stab2536','trainable_parameters':sum(p.numel() for p in model.parameters()),
        'normalization':config['normalization'],'adaptations':['local frozen SSAFD inputs at512 instead of author256',
            'FP32 forward with logits/BCEWithLogits','local BCE+Dice recipe; not original paper training reproduction',
            'GroupNorm arm changes only normalization; not an author model']}
