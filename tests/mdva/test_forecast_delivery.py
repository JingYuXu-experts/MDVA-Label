import hashlib
import json

import pytest

from mdva.training.forecast_delivery import lock, registered_results


def test_formal_forecast_test_waits_for_complete_bound_experiments(tmp_path):
    dataset = tmp_path/'dataset'; dataset.mkdir()
    raw = b''
    (dataset/'forecast-ready.jsonl').write_bytes(raw)
    manifest = json.dumps({'forecast-ready.jsonl': hashlib.sha256(raw).hexdigest()}).encode()
    (dataset/'manifest-hashes.json').write_bytes(manifest)
    data_hash = hashlib.sha256(manifest).hexdigest()
    experiment = tmp_path/'experiment'; experiment.mkdir()
    (experiment/'protocol.json').write_text(json.dumps({'dataset_manifest_sha256': data_hash}))
    cv = experiment/'forecast/temporal-cv'; cv.mkdir(parents=True)
    (cv/'summary.json').write_text(json.dumps({'fold_results': [{'state': 'completed'}]*18,
                                             'mean_ap': {'lstm': .2, 'mlp': .2}}))
    for name in registered_results():
        file = experiment/'forecast'/name; file.parent.mkdir(exist_ok=True)
        neural = file.name == 'result.json'
        record = {'data': data_hash, 'chosen': {'mean_cv_ap': .1, 'ap': .1}}
        if neural: record['kind'] = 'lstm'
        file.write_text(json.dumps(record))
        weights = file.parent/('weights.safetensors' if neural else file.name.replace('-result.json','.joblib'))
        weights.write_bytes(b'not loaded by freeze')
    missing = experiment/'forecast'/registered_results()[-1]
    content = missing.read_bytes(); missing.unlink()
    with pytest.raises(FileNotFoundError): lock(experiment)
    assert not (experiment/'forecast-selection-lock.json').exists()
    missing.write_bytes(content)
    result = lock(experiment)
    assert len(result['results']) == 31 and result['operational_release'] is False
    assert len(result['planned_comparisons']) == 8
    with pytest.raises(ValueError, match='禁止'): lock(experiment)
    torch = pytest.importorskip('torch', reason='篡改防护检查使用模型环境加载器')
    from mdva.training.forecast_delivery import evaluate
    first = experiment/'forecast'/registered_results()[0]
    first.write_bytes(first.read_bytes()+b' ')
    with pytest.raises(ValueError, match='已改变'): evaluate(experiment, dataset)


def test_polygon_candidates_report_holes_and_keep_coordinates_in_native_bounds():
    pytest.importorskip('cv2', reason='候选轮廓测试使用模型环境OpenCV')
    import numpy as np
    from mdva.training.inference import polygons
    probability = np.zeros((32, 32), np.float32)
    probability[2:15, 2:15] = .9
    probability[5:10, 5:10] = 0
    probability[20:29, 21:31] = .8
    anns, skipped = polygons(probability, 'FL', 'example')
    assert len(anns) == 1 and len(skipped) == 1
    assert '孔洞' in skipped[0]['reason']
    assert anns[0]['source'] == 'preannotate'
    assert all(0 <= coordinate < 32 for coordinate in anns[0]['coords'])
    assert anns[0]['confidence'] == pytest.approx(.8)


def test_catalogue_downloader_refuses_arbitrary_files_before_network():
    from mdva.web.space_weather import download_flare_catalogue
    for name in ('../../secret.csv', 'http://localhost/private', 'sci_xrsf-l2-flrpt_geo_y2026_v1-0-1.csv'):
        with pytest.raises(ValueError): download_flare_catalogue(name)
