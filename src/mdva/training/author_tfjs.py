"""Read the pinned author's TFJS float arrays as an inference-only PyTorch DAG.

No Keras deserialization, pickle, dynamic imports, training or implicit sigmoid.
This importer is specific to the independently fingerprinted 2018 browser model.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F

from .author_model_snapshot import COMMIT, MODEL_BLOB, PINNED_TREE_SHA256, git_blob, tree_digest
from .runtime import digest


class NativeTranspose(nn.ConvTranspose2d):
    def forward(self, x):
        # TF SAME for odd kernel 3 / stride 2 crops the trailing row/column.
        return super().forward(x)[..., :x.shape[-2]*2, :x.shape[-1]*2]


def read_snapshot(directory):
    directory = Path(directory).resolve()
    snapshot = json.loads((directory/'snapshot.json').read_text(encoding='utf-8'))
    if snapshot['commit'] != COMMIT or snapshot['model_git_blob'] != MODEL_BLOB:
        raise ValueError('只接受登记的作者模型提交')
    files = [snapshot['model_source'], *snapshot['weight_sources']]
    if tree_digest([{'path':'keras/'+s['filename'],'sha':s['git_blob'],'size':s['bytes'],'type':'blob'} for s in files]) != PINNED_TREE_SHA256:
        raise ValueError('权重清单与独立锁定的作者提交不符')
    raw = (directory/'model.json').read_bytes()
    if git_blob(raw) != MODEL_BLOB or digest(directory/'model.json') != snapshot['model_source']['sha256']:
        raise ValueError('模型结构身份改变')
    model = json.loads(raw)
    sources = {s['filename']: s for s in snapshot['weight_sources']}
    tensors = {}
    for group in model['weightsManifest']:
        pieces = []
        for name in group['paths']:
            if Path(name).name != name or name not in sources:
                raise ValueError('权重路径未登记')
            path = directory/name
            if path.is_symlink() or path.resolve().parent != directory:
                raise ValueError('权重路径越界')
            raw = path.read_bytes(); source = sources[name]
            if len(raw) != source['bytes'] or git_blob(raw) != source['git_blob'] or digest(path) != source['sha256']:
                raise ValueError('作者权重身份改变')
            pieces.append(raw)
        raw = b''.join(pieces); offset = 0
        for item in group['weights']:
            if item['dtype'] != 'float32' or item['name'] in tensors:
                raise ValueError('不支持的权重类型或重复名称')
            count = int(np.prod(item['shape'])); end = offset+count*4
            values = np.frombuffer(raw[offset:end], '<f4').copy()
            if values.size != count or not np.isfinite(values).all():
                raise ValueError('权重长度或数值异常')
            tensors[item['name']] = values.reshape(item['shape'])
            offset = end
        if offset != len(raw): raise ValueError('权重分片长度异常')
    return model, tensors, snapshot


class BrowserCNN193(nn.Module):
    """Exact pinned Keras inference graph, linear native output (not probability)."""
    def __init__(self, model, tensors):
        super().__init__()
        config = model['modelTopology']['model_config']['config']
        self.graph = config['layers']
        self.layers = nn.ModuleDict()
        self.input_name = config['input_layers'][0][0]
        self.output_name = config['output_layers'][0][0]
        used = set(); channels = {}; known = set()
        for layer in self.graph:
            name, kind, c = layer['name'], layer['class_name'], layer['config']
            if name in known: raise ValueError('重复的图层名称')
            incoming = [n[0] for n in layer['inbound_nodes'][0]] if layer['inbound_nodes'] else []
            if any(n not in known for n in incoming): raise ValueError('图层前驱无效')
            if kind == 'InputLayer':
                if name != self.input_name or c['batch_input_shape'] != [None,256,256,1] or incoming:
                    raise ValueError('非登记输入')
                channels[name] = 1
            elif kind in ('Conv2D','Conv2DTranspose'):
                if len(incoming) != 1 or c['kernel_size'] != [3,3] or c['padding'] != 'same' or c['activation'] != 'linear' or not c['use_bias'] or c['data_format'] != 'channels_last':
                    raise ValueError('非登记卷积定义')
                if c.get('dilation_rate', [1,1]) != [1,1]: raise ValueError('不支持膨胀卷积')
                inputs = channels[incoming[0]]; outputs = c['filters']
                if kind == 'Conv2D':
                    if c['strides'] != [1,1]: raise ValueError('非登记卷积步长')
                    op = nn.Conv2d(inputs,outputs,3,padding=1)
                else:
                    if c['strides'] != [2,2]: raise ValueError('非登记转置卷积步长')
                    op = NativeTranspose(inputs,outputs,3,stride=2)
                kernel_name, bias_name = name+'/kernel', name+'/bias'
                weight = torch.from_numpy(tensors[kernel_name].transpose(3,2,0,1).copy())
                bias = torch.from_numpy(tensors[bias_name].copy())
                if weight.shape != op.weight.shape or bias.shape != op.bias.shape:
                    raise ValueError('卷积权重尺寸错误')
                with torch.no_grad(): op.weight.copy_(weight); op.bias.copy_(bias)
                self.layers[name] = op; channels[name] = outputs
                used.update((kernel_name,bias_name))
            elif kind == 'Activation':
                if len(incoming) != 1 or c['activation'] != 'elu': raise ValueError('非登记激活')
                channels[name] = channels[incoming[0]]
            elif kind == 'MaxPooling2D':
                if len(incoming) != 1 or c['pool_size'] != [2,2] or c['strides'] != [2,2] or c['padding'] != 'valid':
                    raise ValueError('非登记池化')
                channels[name] = channels[incoming[0]]
            elif kind == 'Dropout':
                if len(incoming) != 1 or c['rate'] != .15: raise ValueError('非登记dropout')
                channels[name] = channels[incoming[0]]
            elif kind == 'Concatenate':
                if len(incoming) != 2 or c['axis'] != -1: raise ValueError('非登记拼接')
                channels[name] = sum(channels[n] for n in incoming)
            else: raise ValueError('不支持的作者图层')
            known.add(name)
        if used != set(tensors) or channels.get(self.output_name) != 1 or len(self.graph) != 67:
            raise ValueError('图或权重未完全消费')
        self.requires_grad_(False)
        self.eval()

    def train(self, mode=True):
        if mode: raise ValueError('作者权重转换只允许推理')
        return super().train(False)

    def forward(self, x, *, capture=(), synthetic=False):
        if x.ndim != 4 or x.shape[0] != 1 or x.shape[1] != 1 or (x.shape[-2:] != (256,256) and not synthetic):
            raise ValueError('仅允许单图原生256输入；小尺寸仅用于合成数值校验')
        if x.shape[-2] % 16 or x.shape[-1] % 16 or not torch.isfinite(x).all():
            raise ValueError('无效的输入尺寸或数值')
        if not set(capture).issubset({l['name'] for l in self.graph}): raise ValueError('未知采集图层')
        values = {}
        for layer in self.graph:
            name, kind = layer['name'],layer['class_name']
            incoming = [values[n[0]] for n in layer['inbound_nodes'][0]] if layer['inbound_nodes'] else []
            if kind == 'InputLayer': y = x
            elif kind in ('Conv2D','Conv2DTranspose'): y = self.layers[name](incoming[0])
            elif kind == 'Activation': y = F.elu(incoming[0])
            elif kind == 'MaxPooling2D': y = F.max_pool2d(incoming[0],2)
            elif kind == 'Dropout': y = incoming[0]
            elif kind == 'Concatenate': y = torch.cat(incoming,dim=1)
            values[name] = y
        output = values[self.output_name]
        if not torch.isfinite(output).all(): raise ValueError('作者模型输出非有限')
        return (output,{n: values[n] for n in capture}) if capture else output


def load(directory, device='cpu'):
    model,tensors,snapshot = read_snapshot(directory)
    return BrowserCNN193(model,tensors).to(device),snapshot


def native_binary(output):
    """Browser Math.round(value)===1, not sigmoid or a calibrated probability."""
    values = np.asarray(output)
    if not np.isfinite(values).all(): raise ValueError('非有限输出')
    return np.floor(values+.5) == 1
