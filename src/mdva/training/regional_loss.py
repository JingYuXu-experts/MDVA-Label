"""Capped connected-region weighting, preserving foreground/background loss mass.

This is a local training hypothesis, not physical-instance weighting. Small
connected foreground regions receive more BCE weight; total foreground weight
is unchanged per image. Global Dice and background BCE remain unchanged.
"""
import numpy as np
from scipy import ndimage
import torch
from torch.nn import functional as F


def weight_map(target):
    array=np.asarray(target)
    if array.ndim!=2 or not np.isfinite(array).all() or not np.isin(array,[0,1]).all():
        raise ValueError('区域权重需要二维有限二值参考')
    labels,n=ndimage.label(array>0,structure=np.ones((3,3)))
    weights=np.ones(array.shape,dtype=np.float32)
    if n<=1:return weights
    counts=np.bincount(labels.ravel())[1:].astype(np.float64)
    raw=np.minimum(4.,np.sqrt(counts.max()/counts))
    raw/=np.dot(raw,counts)/counts.sum()
    lookup=np.concatenate(([1.],raw))
    return lookup[labels].astype(np.float32)


def loss_value(logits,target,kind):
    if kind!='bce_dice':raise ValueError('区域对照固定BCE+Dice')
    # Validation and scheduler always use the identical unweighted objective.
    if torch.is_grad_enabled():
        arrays=target.detach().cpu().numpy()
        maps=np.stack([weight_map(a[0]) for a in arrays])[:,None]
        weights=torch.from_numpy(maps).to(logits.device)
        bce=(F.binary_cross_entropy_with_logits(logits.float(),target.float(),reduction='none')*weights).mean()
    else:bce=F.binary_cross_entropy_with_logits(logits.float(),target.float())
    p,y=logits.float().sigmoid().flatten(1),target.float().flatten(1)
    dice=1-((2*(p*y).sum(1)+1)/(p.sum(1)+y.sum(1)+1)).mean()
    return .5*bce+.5*dice
