"""Replay completed FL learning controls and expose real training-only cases."""
import argparse,base64,gc,html,json
from pathlib import Path
import numpy as np
from PIL import Image,ImageDraw,ImageFont
from .durable import atomic_json,commit
from .runtime import digest,exclusive,output_path,now,Resources
from .datasets import SegmentationDataset
from .fl_learning_study import code,ARMS,select_training,EXPERIMENT,ROOT
from .fl_author_model import AuthorGraph


def run(study,output):
    import torch
    from safetensors.torch import save
    from .reviewed_train import load_checkpoint,seed_all
    from .metrics import binary_segmentation
    study=Path(study).resolve();p=json.loads((study/'protocol.json').read_text(encoding='utf-8'))
    summary=json.loads((study/'summary.json').read_text(encoding='utf-8'))
    if summary['state']!='completed' or summary['protocol_sha256']!=digest(study/'protocol.json') or p['code']!=code():raise ValueError('先完成所有固定对照并核验身份')
    output=output_path(output,p['dataset'])
    if output.exists():raise ValueError('复核使用新目录，不覆盖')
    data=SegmentationDataset(p['dataset'],'FL','train',512)
    rows=select_training(json.loads((EXPERIMENT/'fl-review-samples.json').read_text(encoding='utf-8')),data.rows)
    if [r['uri'] for r in rows]!=p['uris']:raise ValueError('样本集合改变')
    data.rows=rows;items=[data[i] for i in range(16)];predictions={};identities={}
    output.mkdir(parents=True);normalization_probe={}
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        seed_all(2026);torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
        torch.cuda.set_per_process_memory_fraction(.75);resource=Resources()
        for arm,norm,_ in ARMS:
            path=study/arm/'last.pt';saved=load_checkpoint(path)
            if saved['step']!=300 or saved['identity']!={'protocol_sha256':digest(study/'protocol.json'),'arm':arm}:raise ValueError('最终对照检查点身份不符')
            model=AuthorGraph(norm);model.load_state_dict(saved['model']);model.cuda().eval();predictions[arm]=[]
            raw=save({k:v.cpu().contiguous() for k,v in saved['model'].items()})
            commit(output/(arm+'.safetensors'),lambda stream:stream.write(raw))
            identities[arm]={'checkpoint_sha256':digest(path),'safe_weight_sha256':digest(output/(arm+'.safetensors'))}
            with torch.inference_mode():
                for i,item in enumerate(items):
                    logits=model(item['image'][None].cuda())
                    if not torch.isfinite(logits).all():raise ValueError('输出非有限')
                    probability=logits.sigmoid()[0,0].cpu().numpy();prediction=probability>=.5
                    score=binary_segmentation(prediction,item['semantic_mask'][0].numpy()>0,boundary=True)
                    original=saved['history'][-1]['per_image'][i]
                    if original['uri']!=rows[i]['uri'] or any(score[k]!=original[k] for k in ('tp','fp','fn')):raise ValueError('训练样本评价复算不一致')
                    predictions[arm].append((prediction,score))
            if norm=='batch':
                from .metrics import segmentation_summary
                before={k:v.detach().clone() for k,v in model.state_dict().items()}
                layers=[m for m in model.modules() if isinstance(m,torch.nn.BatchNorm2d)]
                for layer in layers:layer.train();layer.track_running_stats=False
                scores=[]
                with torch.inference_mode():
                    for item in items:
                        logits=model(item['image'][None].cuda())
                        if not torch.isfinite(logits).all():raise ValueError('单图统计输出非有限')
                        prediction=logits.sigmoid()[0,0].cpu().numpy()>=.5
                        scores.append(binary_segmentation(prediction,item['semantic_mask'][0].numpy()>0,boundary=True))
                if not all(torch.equal(v,model.state_dict()[k]) for k,v in before.items()):raise ValueError('统计诊断改动了权重或缓冲区')
                normalization_probe[arm]={'running_stats':summary['results'][arm]['metrics']['overall'],
                    'single_image_stats':segmentation_summary(scores),'weights_and_buffers_unchanged':True,
                    'scope':'Inference-only training-sample diagnosis. Per-image batch statistics are not a validated deployment policy or independent accuracy.'}
                del before
            resource.sample();model.cpu();del model,saved;gc.collect();torch.cuda.empty_cache()
    font=lambda n:ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',n)
    def overlay(rgb,mask,color):
        pixels=np.asarray(rgb,dtype=float).copy();pixels[mask]=.5*pixels[mask]+.5*np.asarray(color)
        return Image.fromarray(pixels.astype('uint8'))
    entries=[];blocks=[]
    for i,row in enumerate(rows):
        source=data.source(row['uri']);rgb=Image.open(source).convert('RGB');reference=items[i]['semantic_mask'][0].numpy()>0
        panels=[rgb,overlay(rgb,reference,(30,210,100))];titles=['原图','参考标注：绿色']
        for arm,_,_ in ARMS:
            mask,score=predictions[arm][i];panels.append(overlay(rgb,mask,(25,210,225)));titles.append(arm+' '+format(score['dice'],'.2%'))
        canvas=Image.new('RGB',(3192,660),'#f2f5f9');draw=ImageDraw.Draw(canvas)
        draw.text((16,12),f"训练样本{i+1:02d} · {row['instrument']} · {row['time_utc']}",font=font(27),fill='#172d43')
        for j,(panel,title) in enumerate(zip(panels,titles)):
            x=16+j*530;draw.text((x,62),title,font=font(22),fill='#172d43');canvas.paste(panel.resize((512,512)),(x,102))
        draw.text((16,625),'仅为16张已见训练图；不能当作验证、测试成绩或真实使用识别率。掩膜只用于训练和评价。',font=font(21),fill='#172d43')
        target=output/f'case-{i+1:02d}.png';canvas.save(target)
        entries.append({'uri':row['uri'],'image_sha256':digest(source),'file':target.name,'sha256':digest(target)})
        encoded=base64.b64encode(target.read_bytes()).decode();blocks.append(f'<article><h2>训练样本{i+1:02d}</h2><code>{html.escape(row["uri"])}</code><p><img src="data:image/png;base64,{encoded}" onclick="this.classList.toggle(\'zoom\')" alt="原图、参考及四组真实预测"></p></article>')
    page='<!doctype html><meta charset="utf-8"><title>暗条可学习性真实对照</title><style>body{font:16px/1.7 system-ui;background:#f2f5f9;color:#172d43;margin:24px}article{background:white;padding:16px;margin:20px 0}img{width:100%;cursor:zoom-in}img.zoom{width:3192px;max-width:none;cursor:zoom-out}code{overflow-wrap:anywhere}</style><h1>暗条：16张训练图的真实对照</h1><p>全部为已见训练图，只有可学习性意义，没有独立识别性能结论。none=无归一化；batch=BatchNorm；w1/w10=正类损失权重。绿色参考、青色预测；点击图像放大查看细结构。</p>'+''.join(blocks)
    (output/'打开看暗条学习对照.html').write_text(page,encoding='utf-8')
    result={'created_utc':now(),'state':'completed','scope':'16 fixed FL training images only; not validation or test','study_summary_sha256':digest(study/'summary.json'),'code_sha256':digest(__file__),
        'checkpoints':identities,'normalization_probe':normalization_probe,'exact_count_replay':True,'cases':entries,'training_started':False,'test_accessed':False,'models_replaced':False}
    atomic_json(output/'summary.json',result);return result


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment();p=argparse.ArgumentParser();p.add_argument('--study',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    r=run(a.study,a.output);print(json.dumps({'state':r['state'],'cases':len(r['cases'])}))
