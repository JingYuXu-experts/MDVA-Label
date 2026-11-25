"""Readable fixed and failure-case comparisons from a verified validation review."""
from __future__ import annotations
import argparse,base64,html,json
from pathlib import Path
import numpy as np
from PIL import Image,ImageDraw,ImageFont
from .datasets import SegmentationDataset
from .diagnostic_support import sample_id
from .runtime import digest
from .durable import atomic_json
ROOT=Path(__file__).resolve().parents[3]


def select(rows):
    if not rows or len({r['uri'] for r in rows})!=len(rows):raise ValueError('案例为空或重复')
    chosen={}
    def add(row,reason):chosen.setdefault(row['uri'],{'row':row,'reasons':[]})['reasons'].append(reason)
    for year in sorted({r['year'] for r in rows}):
        group=sorted([r for r in rows if r['year']==year],key=lambda r:(r['control_dice'],r['uri']))
        for q,label in ((.1,'较差'),(.5,'中位'),(.9,'较好')):
            add(group[round(q*(len(group)-1))],year+'年：按原候选分数固定选择'+label+'案例')
    ordered=sorted(rows,key=lambda r:(r['new_dice']-r['control_dice'],r['uri']))
    for row in ordered[:3]:add(row,'事后错误诊断：退步最大组')
    for row in ordered[-3:]:add(row,'事后错误诊断：改善最大组')
    return list(chosen.values())


def overlay(image,mask,color):
    a=np.asarray(image.convert('RGB'),float).copy();a[mask]=.5*a[mask]+.5*np.array(color)
    return Image.fromarray(a.astype('uint8'))


def build(study,review):
    study,review=Path(study),Path(review)
    summary=json.loads((review/'summary.json').read_text(encoding='utf-8'))
    if summary['state']!='completed' or not summary['exact_replay'] or summary['test_accessed']:raise ValueError('必须先完成训练停止后的验证复算')
    if summary['identity']['training_protocol_sha256']!=digest(study/'protocol.json'):raise ValueError('研究协议改变')
    protocol=json.loads((study/'protocol.json').read_text(encoding='utf-8'))
    data=SegmentationDataset(protocol['dataset'],'CH','validation');known={r['uri']:r for r in data.rows}
    baseline=ROOT/'out/recognition-improvement-20261116/display-probe-01'
    index=[];records={}
    for row in data.rows:
        folder=review/'cases'/sample_id(row['uri']);record=json.loads((folder/'result.json').read_text(encoding='utf-8'))
        if record['identity_sha256']!=digest(review/'identity.json'):raise ValueError('逐图评价身份不同')
        records[row['uri']]=record
        index.append({'uri':row['uri'],'year':row['time_utc'][:4],'new_dice':record['score']['dice'],'control_dice':record['control_score']['dice']})
    output=review/'case-viewer'
    if output.exists():raise ValueError('案例版本已存在，不覆盖')
    output.mkdir();selected=select(index);items=[]
    font=lambda n,b=False:ImageFont.truetype('C:/Windows/Fonts/msyhbd.ttc' if b else 'C:/Windows/Fonts/msyh.ttc',n)
    for number,item in enumerate(selected,1):
        row=item['row'];native=known[row['uri']];sid=sample_id(row['uri']);record=records[row['uri']]
        folder=review/'cases'/sid
        if any(digest(folder/n)!=sha for n,sha in record['assets'].items()):raise ValueError('评价缓存改变')
        source=data.source(row['uri'])
        if digest(source)!=record['image_sha256']:raise ValueError('案例原图改变')
        image=Image.open(source).convert('RGB');reference=np.load(data.source(native['mask_uri']),allow_pickle=False)>0
        new=np.asarray(Image.open(folder/'prediction.png'))>0
        old_folder=baseline/'cases'/sid;old_record=json.loads((old_folder/'result.json').read_text(encoding='utf-8'))
        if digest(old_folder/'original.png')!=old_record['assets']['original.png']:raise ValueError('原候选案例缓存改变')
        old=np.asarray(Image.open(old_folder/'original.png'))>0
        errors=np.zeros((*reference.shape,3),dtype='uint8');errors[new&reference]=(35,180,105);errors[~new&reference]=(245,75,75);errors[new&~reference]=(65,135,240)
        images=[image,overlay(image,reference,(30,210,115)),overlay(image,old,(255,195,30)),overlay(image,new,(30,210,215)),Image.fromarray(errors)]
        titles=['原图', '参考：绿色',f"原候选：{row['control_dice']:.2%}",f"新模型：{row['new_dice']:.2%}",'新模型错分区域']
        canvas=Image.new('RGB',(1740,550),'#f2f5f9');draw=ImageDraw.Draw(canvas)
        draw.text((20,12),f"案例{number:02d} · CH验证 · {native['time_utc']}",font=font(26,True),fill='#172d43')
        for k,(im,title) in enumerate(zip(images,titles)):
            x=20+k*344;draw.text((x,65),title,font=font(22,True),fill='#172d43')
            canvas.paste(im.resize((324,324),Image.Resampling.LANCZOS),(x,108))
        draw.text((20,450),'错误图：绿色=重合；红色=漏分；蓝色=多分；黑色=双方背景。分数是Dice，不是准确率。',font=font(22),fill='#273e54')
        draw.text((20,490),'仅为开发验证案例；显示缩略图经过缩放，全部分数在原512网格计算。',font=font(22),fill='#273e54')
        file=output/f'case-{number:02d}.png';canvas.save(file)
        items.append({**item,'file':file.name,'sha256':digest(file),'source_sha256':digest(source)})
    metric=summary['metrics'];control=summary['control_metrics']
    parts=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SCSS冕洞真实效果对照</title><style>body{font:16px/1.7 system-ui;margin:24px;background:#f2f5f9;color:#172d43}main{max-width:1740px;margin:auto}article{background:white;padding:18px;border-radius:10px;margin:24px 0}img{width:100%;height:auto;cursor:zoom-in}.zoom{width:1740px;max-width:none;cursor:zoom-out}h1{font-size:26px}code{overflow-wrap:anywhere}table{border-collapse:collapse}td,th{padding:8px 22px;border:1px solid #becbd8}button{padding:8px 16px}summary{cursor:pointer}</style><main><h1>SCSS冕洞：完整验证与真实案例</h1>',
       '<p>当前为重复使用的开发验证集、一个随机种子。没有打开最终测试集，未发布新应用模型。固定案例与事后错误案例明确分开；不要只看漂亮结果。</p>',
       '<table><tr><th>完整526图指标</th><th>原CH候选</th><th>'+html.escape(summary.get('checkpoint_policy_label','SCSS首8轮最佳'))+'</th></tr>']
    for key,label in [('dice','Dice'),('precision','Precision'),('recall','Recall')]:parts.append(f'<tr><td>{label}</td><td>{control[key]:.2%}</td><td>{metric[key]:.2%}</td></tr>')
    parts.append('</table><p>点击图像可放大。参考标注只用于评价，模型推理没有读取参考。</p>')
    for number,item in enumerate(items,1):
        encoded=base64.b64encode((output/item['file']).read_bytes()).decode()
        parts.append(f'<article><h2>案例{number:02d}</h2><p>{html.escape("；".join(item["reasons"]))}</p><code>{html.escape(item["row"]["uri"])}</code><p><img alt="原图、参考、原候选、新模型及错分区域对照" src="data:image/png;base64,{encoded}" onclick="this.classList.toggle(\'zoom\')"></p></article>')
    parts.append('</main></html>');(output/'打开看真实效果.html').write_text(''.join(parts),encoding='utf-8')
    atomic_json(output/'selection.json',{'review_sha256':digest(review/'summary.json'),'rule':'per-year baseline-score quantiles 0.1/0.5/0.9 plus posthoc3 largest gains and3 regressions; duplicate URIs combined','items':items,'evaluator_sha256':digest(__file__)})
    return {'cases':len(items),'viewer':str(output/'打开看真实效果.html')}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--study',required=True);p.add_argument('--review',required=True);a=p.parse_args();print(json.dumps(build(a.study,a.review),ensure_ascii=False))
