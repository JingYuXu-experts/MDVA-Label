"""Read existing monitor health records; a time span is not proof of uptime."""
from pathlib import Path
import json
import sqlite3
from datetime import datetime,timezone


def inspect(database):
    connection=sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        sources=[]
        for source, in connection.execute('SELECT DISTINCT source FROM health_sample'):
            rows=connection.execute('SELECT t,error FROM health_sample WHERE source=? ORDER BY t',(source,)).fetchall()
            gaps=[b[0]-a[0] for a,b in zip(rows,rows[1:])]
            failures=sum(bool(r[1]) for r in rows)
            sources.append({'source':source,'checks':len(rows),'failed_checks':failures,
                'check_success_fraction':1-failures/len(rows),'first_utc':datetime.fromtimestamp(rows[0][0],timezone.utc).isoformat(),
                'last_utc':datetime.fromtimestamp(rows[-1][0],timezone.utc).isoformat(),
                'span_days':(rows[-1][0]-rows[0][0])/86400,'maximum_gap_seconds':max(gaps,default=None)})
        count,bytes_total=connection.execute('SELECT COUNT(*),COALESCE(SUM(LENGTH(payload)),0) FROM document').fetchone()
        seven_days=bool(sources) and all(r['span_days']>=7 for r in sources)
        return {'created_utc':datetime.now(timezone.utc).isoformat(),'sources':sources,
            'raw_documents':count,'raw_document_payload_bytes':bytes_total,
            'seven_day_record_span_reached':seven_days,'continuous_uptime_verified':False,
            'state':'requires_gap_and_growth_review' if seven_days else 'waiting_for_seven_day_window',
            'limitations':['检查成功比例不等于连续在线率','最大间断须结合服务重启、退避和上游失效解释',
                           '单次缓存快照不足以证明长期增长和保留策略','当前未设置新的提醒或自动训练任务']}
    finally:connection.close()


if __name__=='__main__':
    import argparse
    from ..training.durable import atomic_json
    p=argparse.ArgumentParser();p.add_argument('--database',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();atomic_json(a.output,inspect(a.database))
