import csv
import json
import sqlite3

import pytest

from mdva.training.human_trial import FIELDS,analyze,template
from mdva.web.monitor_acceptance import inspect


def test_blank_trial_never_claims_savings_and_preserves_operator_file(tmp_path):
    cases=tmp_path/'cases.json';cases.write_text(json.dumps({'cases':[{'sample_id':'a','category':'AR','uri':'x.jpg'}]}))
    target=tmp_path/'trial.csv';template(cases,target)
    result=analyze(target)
    assert result['completed_pairs']==0 and 'median_reduction_fraction' not in result
    assert result['state']=='pending_human_measurements'
    with pytest.raises(ValueError,match='禁止覆盖'):template(cases,target)


def test_partial_trial_and_invalid_quality_do_not_pass(tmp_path):
    target=tmp_path/'trial.csv'
    row={'case_id':'a','category':'AR','uri':'x','operator_id':'person1','order':'manual_first',
         'manual_seconds':'100','assisted_seconds':'70','manual_quality_dice':'.9','assisted_quality_dice':'.91',
         'manual_severe_misses':'0','assisted_severe_misses':'0'}
    def write(value):
        with target.open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=FIELDS);writer.writeheader();writer.writerow(value)
    write(row);value=analyze(target)
    assert value['completed_pairs']==1 and not value['quality_scores_independently_verified']
    write({**row,'assisted_quality_dice':'NaN'})
    with pytest.raises(ValueError):analyze(target)


def test_health_span_is_not_claimed_as_continuous_uptime(tmp_path):
    path=tmp_path/'monitor.sqlite';c=sqlite3.connect(path)
    c.executescript('CREATE TABLE health_sample(source TEXT,t REAL,error TEXT); CREATE TABLE document(payload TEXT);')
    c.executemany('INSERT INTO health_sample VALUES(?,?,?)',[('s',0,''),('s',8*86400,'timeout')]);c.commit();c.close()
    value=inspect(path)
    assert value['seven_day_record_span_reached'] and not value['continuous_uptime_verified']
    assert value['sources'][0]['check_success_fraction']==.5
