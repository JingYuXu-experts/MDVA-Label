"""Import measured human trial pairs; never invent missing time/quality values."""
from pathlib import Path
import argparse
import csv
import json
import math
import statistics

from .durable import atomic_json
from .runtime import digest,now

FIELDS=['case_id','category','uri','operator_id','order','manual_seconds','assisted_seconds',
        'manual_quality_dice','assisted_quality_dice','manual_severe_misses','assisted_severe_misses','notes']


def template(cases,destination):
    if Path(destination).exists():raise ValueError('计时模板已存在，禁止覆盖人工记录')
    data=json.loads(Path(cases).read_text(encoding='utf-8'))
    with Path(destination).open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=FIELDS);writer.writeheader()
        for row in data['cases']:
            writer.writerow({'case_id':row['sample_id'],'category':row['category'],'uri':row['uri']})


def analyze(source):
    with Path(source).open(encoding='utf-8-sig',newline='') as stream:rows=list(csv.DictReader(stream))
    ids=[r['case_id'] for r in rows]
    if len(set(ids))!=len(ids):raise ValueError('试用图像重复')
    complete=[]
    for row in rows:
        columns=FIELDS[3:-1]
        if not all(row.get(k,'').strip() for k in columns):continue
        if row['order'] not in ('manual_first','assisted_first'):raise ValueError('顺序仅允许manual_first或assisted_first')
        values={k:float(row[k]) for k in FIELDS[5:-1]}
        if not all(math.isfinite(v) for v in values.values()):raise ValueError('计时和质量值必须有限')
        if any(values[k]<=0 for k in ('manual_seconds','assisted_seconds')):raise ValueError('耗时必须大于0')
        if any(not 0<=values[k]<=1 for k in ('manual_quality_dice','assisted_quality_dice')):raise ValueError('Dice应为0—1')
        if any(values[k]<0 or not values[k].is_integer() for k in ('manual_severe_misses','assisted_severe_misses')):raise ValueError('严重漏标数必须为非负整数')
        complete.append({**row,**values})
    result={'updated_utc':now(),'source_sha256':digest(source),'planned_images':len(rows),'completed_pairs':len(complete),
        'state':'pending_human_measurements','single_operator':len({r['operator_id'] for r in complete})<=1,
        'scope':'仅本次实际参与者与样本；顺序、熟悉度与质量参考需单独说明，不能推广为普遍提效',
        'quality_scores_independently_verified':False}
    if complete:
        manual=statistics.median(r['manual_seconds'] for r in complete)
        assisted=statistics.median(r['assisted_seconds'] for r in complete)
        result.update(median_manual_seconds=manual,median_assisted_seconds=assisted,
            paired_median_time_reduction=statistics.median(1-r['assisted_seconds']/r['manual_seconds'] for r in complete),
            median_time_reduction=1-assisted/manual,
            mean_quality_difference=statistics.mean(r['assisted_quality_dice']-r['manual_quality_dice'] for r in complete),
            severe_misses_difference=sum(r['assisted_severe_misses']-r['manual_severe_misses'] for r in complete))
    if len(complete)==len(rows) and len(rows)==60:
        result['state']='measurements_complete_awaiting_quality_review'
        result['engineering_goal_met_by_submitted_values']=(result['median_time_reduction']>=.2
            and result['mean_quality_difference']>=0 and result['severe_misses_difference']<=0)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--template',action='store_true');a=p.parse_args()
    if a.template:template(a.input,a.output)
    else:atomic_json(a.output,analyze(a.input))
