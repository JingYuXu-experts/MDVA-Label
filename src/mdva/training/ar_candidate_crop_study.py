"""Compare image-based local AR verification with geometry on the same frozen candidates."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import find_objects, label
import torch
from torch import nn
from torch.nn import functional as F

from .ar_component_verifier import FEATURES
from .ar_hmi_candidate_ranker import tally
from .ar_hmi_temporal_ranker import build_metrics, object_scores, time_partition
from .ar_region_retention import filter_by_area
from .ar_scale_study import BUDGET, ROOT, load_plan, warm_model
from .ar_sharp_auxiliary import dual_head
from .datasets import SegmentationDataset
from .durable import atomic_checkpoint, atomic_json, commit
from .journal import append, read
from .reviewed_train import load_checkpoint, restore_rng, rng_state, seed_all
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


SIDE = 64
TRAIN_IMAGES = 576
EPOCHS = 16
RETENTION = .98
SEED = 2026
BATCH = 64
LEDGER = ROOT/'out/training-20261112-v2'


class CropRanker(nn.Module):
    def __init__(self):
        super().__init__()
        self.encoder = nn.Sequential(
            nn.Conv2d(3,16,5,stride=2,padding=2), nn.ReLU(),
            nn.Conv2d(16,24,3,stride=2,padding=1), nn.ReLU(),
            nn.Conv2d(24,32,3,stride=2,padding=1), nn.ReLU(),
            nn.Conv2d(32,48,3,stride=2,padding=1), nn.ReLU(),
        )
        self.output = nn.Sequential(nn.Linear(96+len(FEATURES),48),nn.ReLU(),nn.Dropout(.15),nn.Linear(48,1))

    def forward(self, crops, features):
        encoded = self.encoder(crops).flatten(2)
        return self.output(torch.cat((encoded.mean(2),encoded.amax(2),features),dim=1)).squeeze(1)


def resize_patch(array, radius, center, pad, resampling):
    y,x = center
    top,left = y-radius,x-radius
    bottom,right = y+radius,x+radius
    fragment = array[max(0,top):min(bottom,array.shape[0]),max(0,left):min(right,array.shape[1])]
    padded = np.pad(fragment,((max(0,-top),max(0,bottom-array.shape[0])),
                              (max(0,-left),max(0,right-array.shape[1]))),constant_values=pad)
    if padded.shape != (2*radius,2*radius):
        raise ValueError('候选裁片中心或边界越出原图')
    return np.asarray(Image.fromarray(padded).resize((SIDE,SIDE),resampling))


def candidate_inputs(gray, probability, raster):
    probability_bytes = np.rint(np.clip(probability,0,1)*255).astype(np.uint8)
    patches, features, component_info = [], [], []
    for component,window in enumerate(find_objects(raster),1):
        sy,sx = window
        chosen = raster[window] == component
        scores = probability[window][chosen]
        height,width = sy.stop-sy.start,sx.stop-sx.start
        y,x = (sy.start+sy.stop)//2,(sx.start+sx.stop)//2
        radius = max(20,int(np.ceil(.85*max(height,width))))
        center = y,x
        near = raster[max(0,y-radius):min(y+radius,1024),max(0,x-radius):min(x+radius,1024)] == component
        # Crop identification uses only the model's candidate, never the human mask.
        local = np.zeros((2*radius,2*radius),np.uint8)
        top,left = max(0,radius-y),max(0,radius-x)
        local[top:top+near.shape[0],left:left+near.shape[1]] = near.astype(np.uint8)*255
        presence = np.asarray(Image.fromarray(local).resize((SIDE,SIDE),Image.Resampling.NEAREST))
        patches.append(np.stack((
            resize_patch(gray,radius,center,127,Image.Resampling.BILINEAR),
            resize_patch(probability_bytes,radius,center,0,Image.Resampling.BILINEAR),presence,
        )))
        area = len(scores)
        features.append([float(np.log1p(area)),area/(height*width),abs(float(np.log(width/height))),
                         float(np.hypot(y-512,x-512))/512,float(scores.mean()),
                         float(np.quantile(scores,.90)),float(scores.std()),float((scores>=.7).mean())])
        component_info.append({'area':area,'bbox_xyxy':[sx.start,sy.start,sx.stop,sy.stop]})
    return np.asarray(patches,dtype=np.uint8).reshape(-1,3,SIDE,SIDE),features,component_info


def sample_training(data, excluded, count=TRAIN_IMAGES):
    remaining = sorted((r for r in data.rows if r['uri'] not in excluded),key=lambda r:(r['time_utc'],r['uri']))
    if len(remaining)<count or count%24:
        raise ValueError('需24个连续时间段中的独立训练样本')
    rng = np.random.default_rng(SEED)
    indices = [int(i) for group in np.array_split(np.arange(len(remaining)),24)
               for i in sorted(rng.choice(group,size=count//24,replace=False))]
    return [remaining[i] for i in indices]


def inference(model, row, data, index):
    with Image.open(data.source(row['uri'])) as source:
        gray = np.array(source.convert('L'))
    item = data[index]
    reference = item['instance_mask'].numpy()
    if data.size is not None:
        reference = np.load(data.source(row['mask_uri']),allow_pickle=False)
    with torch.autocast('cuda',dtype=torch.float16):
        logits = model(item['image'][None].cuda())
    if not torch.isfinite(logits).all():
        raise ValueError('候选复核源模型产生非有限数值')
    probability = F.interpolate(logits.float().sigmoid(),size=reference.shape,mode='bilinear',
                                align_corners=False)[0,0].cpu().numpy()
    raster,count = label(filter_by_area(probability>=.4,64))
    x,features,parts = candidate_inputs(gray,probability,raster)
    base,geometry,assigned = object_scores(raster,parts,reference,[True]*count)
    payload = {'uri':row['uri'],'time_utc':row['time_utc'],'baseline':base,'geometry':geometry,
               'parts':parts,'features':features,'matches':[i in assigned for i in range(count)]}
    return x,payload


def build_cache(directory, data, model, resource, baseline=None):
    rows = []
    for index,row in enumerate(data.rows):
        path = directory/f'{index:04d}.npz'
        metadata = path.with_suffix('.json')
        value = read(metadata)
        if value is None:
            x,value = inference(model,row,data,index)
            commit(path,lambda stream:np.savez_compressed(stream,crops=x))
            value['cache_sha256'] = digest(path)
            atomic_json(metadata,value)
        if value['uri']!=row['uri'] or digest(path)!=value['cache_sha256']:
            raise ValueError('局部复核图像缓存指纹不符')
        if baseline is not None and value['baseline']!=baseline[row['uri']]:
            raise ValueError('新候选没有复现辅助模型原开发指标')
        rows.append({**value,'crops':str(path)})
        if (index+1)%24==0:
            atomic_json(directory.parent/'progress.json',{'state':'caching','split':row['split'],'done':index+1,'total':len(data)})
            resource.sample()
    return rows


def matrices(rows, load_crops=True):
    fields = np.asarray([v for row in rows for v in row['features']],dtype=np.float32).reshape(-1,len(FEATURES))
    labels = np.asarray([v for row in rows for v in row['matches']],dtype=bool)
    arrays = []
    if load_crops:
        for row in rows:
            with np.load(row['crops'],allow_pickle=False) as saved:
                crops = saved['crops']
            if len(crops)!=len(row['matches']):
                raise ValueError('候选裁片和匹配记录数量不符')
            arrays.append(crops)
    return np.concatenate(arrays) if arrays else None,fields,labels


def train_network(directory,x,g,y,protocol,resource):
    seed_all(SEED)
    model = CropRanker().cuda()
    optimizer = torch.optim.AdamW(model.parameters(),lr=.001,weight_decay=.0001)
    checkpoint = directory/'crop-last.pt'
    history,start = [],0
    if checkpoint.is_file():
        saved = load_checkpoint(checkpoint)
        if saved['protocol_sha256']!=protocol:
            raise ValueError('分类检查点不对应当前冻结任务')
        model.load_state_dict(saved['model'])
        optimizer.load_state_dict(saved['optimizer'])
        restore_rng(saved['rng'])
        history,start = saved['history'],saved['epoch']
    x,g,y = (torch.from_numpy(a) for a in (x,g,y))
    imbalance = torch.tensor(int((~y).sum())/max(1,int(y.sum())),device='cuda')
    model.train()
    for epoch in range(start+1,EPOCHS+1):
        if (directory/'PAUSE').is_file():
            return None
        order = torch.randperm(len(y),generator=torch.Generator().manual_seed(SEED+epoch))
        losses = []
        for index in order.split(BATCH):
            optimizer.zero_grad(set_to_none=True)
            prediction = model(x[index].cuda().float()/255,g[index].cuda())
            loss = F.binary_cross_entropy_with_logits(prediction,y[index].cuda().float(),pos_weight=imbalance)
            if not torch.isfinite(loss):
                raise ValueError('局部复核训练损失非有限')
            loss.backward()
            optimizer.step()
            losses.append(float(loss.detach()))
        history.append({'epoch':epoch,'loss':float(np.mean(losses))})
        atomic_checkpoint(checkpoint,{'protocol_sha256':protocol,'epoch':epoch,'model':model.state_dict(),
                                      'optimizer':optimizer.state_dict(),'history':history,'rng':rng_state()})
        atomic_json(directory/'progress.json',{'state':'training','epoch':epoch,'budget':EPOCHS,
                                               'loss':history[-1]['loss'],'resources':resource.sample()})
    return model.eval()


def scores_for(rows,cnn,linear,scale):
    x,g,y = matrices(rows)
    result = {'mean_probability':g[:,FEATURES.index('mean_probability')],
              'geometry':linear.decision_function(scale.transform(g))}
    x,g = torch.from_numpy(x),torch.from_numpy(scale.transform(g))
    scores = []
    with torch.inference_mode():
        for start in range(0,len(y),BATCH):
            stop = start+BATCH
            scores.append(cnn(x[start:stop].cuda().float()/255,g[start:stop].cuda().float()).cpu().numpy())
    result['crop'] = np.concatenate(scores)
    if not all(np.isfinite(v).all() for v in result.values()):
        raise ValueError('局部复核得分存在非有限值')
    return result,y


def thresholds_for(scores,truth,retention=RETENTION):
    if int(truth.sum())<30:
        raise ValueError('阈值组真实候选太少')
    return {name:float(np.quantile(value[truth],1-retention,method='lower')) for name,value in scores.items()}


def apply_filters(rows,scores,thresholds):
    compared,offset = [],0
    for row in rows:
        length = len(row['matches'])
        objects, decisions = {'original':row['baseline']},{}
        for name in scores:
            values = scores[name][offset:offset+length]
            kept = values>=thresholds[name]
            objects[name] = build_metrics(row['geometry'],kept.tolist())[0]
            decisions[name] = {'kept':kept.tolist(),'scores':values.tolist()}
        compared.append({**row,'objects':objects,'decisions':decisions})
        offset += length
    if any(offset!=len(v) for v in scores.values()):
        raise ValueError('复核结果与对应图像候选错位')
    return compared


def run(study,source,paired):
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import average_precision_score
    from sklearn.preprocessing import StandardScaler

    origin = load_plan(source)
    study = output_path(study,origin['dataset'])
    if (study/'summary.json').is_file() or any(study.is_relative_to(p) for p in (source,paired)):
        raise ValueError('以独立版本运行局部复核研究')
    report, plan = (read(paired/name) for name in ('summary.json','protocol.json'))
    checkpoint = paired/'sharp/run/last.pt'
    if (report['state']!='completed' or report['test_accessed'] or report['calibration_accessed']
            or report['checkpoint_sha256']['sharp']!=digest(checkpoint)
            or report['protocol_sha256']!=digest(paired/'protocol.json')
            or report['images_sha256']!=digest(paired/'images.json')):
        raise ValueError('辅助模型阶段身份未核对')
    state = load_checkpoint(checkpoint)
    config = state['identity']['config']
    if state['epoch']!=BUDGET or state['identity']['code']!=plan['code'] or config['protocol_sha256']!=report['protocol_sha256']:
        raise ValueError('配对训练检查点身份不符')
    train, dev = (SegmentationDataset(origin['dataset'],'AR',split,512) for split in ('train','validation'))
    train.rows = sample_training(train,set(plan['train_uris']))
    baseline = {row['uri']:row['arms']['sharp']['objects'] for row in read(paired/'images.json')}
    if set(baseline)!={row['uri'] for row in dev.rows}:
        raise ValueError('开发对照图像名单不同')
    plan = {'paired_report_sha256':digest(paired/'summary.json'),'source_sha256':digest(source/'protocol.json'),
            'code_sha256':digest(__file__),'fixed_model':digest(checkpoint),'train':[r['uri'] for r in train.rows],
            'candidate':{'min_native_area':64,'pixel_threshold':.4,'matching_iou':.5},
            'schedule':{'epochs':EPOCHS,'retention':RETENTION,'batch':BATCH},
            'train_excludes_recent_segmentation_finetune_images':True,
            'development_is_prior_development_not_final_test':True,'test_accessed':False,'calibration_accessed':False}
    marker = study/'protocol.json'
    if marker.exists() and read(marker)!=plan:
        raise ValueError('恢复前冻结设置已改变')
    atomic_json(marker,plan)
    frozen = dual_head(warm_model(origin,config)[0])
    frozen.load_state_dict(state['model'])
    append(LEDGER,'development','活动区局部候选复核训练',
           '固定单图候选。在不属于最近微调的576张训练期图像中拟合并设门槛。')
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        seed_all(SEED)
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        resource = Resources()
        frozen.cuda().eval()
        with torch.inference_mode():
            training = build_cache(study/'train',train,frozen,resource)
        fitting = time_partition(training)
        x,g,y = matrices(fitting['fit'])
        if min(int(y.sum()),int((~y).sum()))<100:
            raise ValueError('训练局部复核的匹配或未匹配标签不足')
        scale = StandardScaler().fit(g)
        linear = LogisticRegression(C=1,class_weight='balanced',random_state=SEED).fit(scale.transform(g),y)
        model = train_network(study,x,scale.transform(g).astype(np.float32),y,digest(marker),resource)
        if model is None:
            return {'state':'paused'}
        del x,g,y
        scores, labels = scores_for(fitting['policy'],model,linear,scale)
        thresholds = thresholds_for(scores,labels)
        settings = {'thresholds':thresholds,'normalization':{'mean':scale.mean_.tolist(),'scale':scale.scale_.tolist()},
                    'geometric_linear':{'weights':linear.coef_[0].tolist(),'intercept':float(linear.intercept_[0])},
                    'fit':{'images':len(fitting['fit']),'candidates':sum(len(r['matches']) for r in fitting['fit'])},
                    'threshold':{'images':len(fitting['policy']),'candidates':len(labels),'matched':int(labels.sum())}}
        atomic_json(study/'thresholds.json',settings)
        with torch.inference_mode():
            development = build_cache(study/'validation',dev,frozen,resource,baseline)
        outcomes = {}
        for name,rows in (('train_late_holdout',fitting['held']),('development',development)):
            scores,labels = scores_for(rows,model,linear,scale)
            verified = apply_filters(rows,scores,thresholds)
            targets = {'score_ap':{key:float(average_precision_score(labels,value)) for key,value in scores.items()},
                       'objects':{key:tally(verified,key) for key in ('original',*scores)},
                       'images':len(rows),'candidates':len(labels),'true_candidates':int(labels.sum())}
            outcomes[name] = targets
            atomic_json(study/f'{name}.json',verified)
        used = resource.sample()
    latest = outcomes['development']['objects']
    passes = latest['crop']['region_precision']>=.80 and latest['crop']['region_recall']>=.80
    result = {'state':'completed','created_utc':now(),'results':outcomes,'reaches_both_80':passes,
              'protocol_sha256':digest(marker),'checkpoint_sha256':digest(study/'crop-last.pt'),
              'thresholds_sha256':digest(study/'thresholds.json'),'development_sha256':digest(study/'development.json'),
              'resource':used,'test_accessed':False,'calibration_accessed':False,'deployed':False}
    atomic_json(study/'summary.json',result)
    atomic_json(study/'progress.json',{'state':'completed','budget':EPOCHS,'epoch':EPOCHS})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('study','source','paired'):
        parser.add_argument('--'+key,required=True,type=Path)
    configure_environment()
    result = run(**{key:p.resolve() for key,p in vars(parser.parse_args()).items()})
    print(json.dumps({k:result[k] for k in ('state','results','reaches_both_80') if k in result},ensure_ascii=False))


if __name__ == '__main__':
    main()
