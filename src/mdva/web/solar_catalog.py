"""Solar observations, region parameters and event associations. Sources stay read-only."""
from __future__ import annotations

import csv
import json
import re
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

PRODUCT_NAMES = {'structure': '结构标注', 'observation': '标准化观测', 'forecast': '耀斑预报数据'}
SOURCE_NAMES = {'ssafd': 'structure', 'solar_obs': 'observation', 'solar_forecast': 'forecast'}

# Literature/code evidence is distinct from a successfully reproduced model.
BASELINES = [
    {'id': 'spnet', 'task': 'AR / CH / FL', 'name': '原团队 SPNet',
     'paper': 'https://arxiv.org/abs/2508.06892', 'code': '', 'weights': '尚未核验公开权重',
     'inputs': '单模态及配对多模态太阳图像；MAE / Transformer',
     'fit': '与数据集科学任务最接近；需核对原实现、训练划分与预处理', 'state': '待核验实现'},
    {'id': 'cnn193', 'task': 'CH', 'name': 'CNN193 · Illarionov & Tlatov 2018',
     'paper': 'https://doi.org/10.1093/mnras/sty2628',
     'code': 'https://github.com/observethesun/coronal_holes', 'weights': '待核验模型文件',
     'inputs': 'SDO/AIA 193 Å 日面图像，原文 1K 分辨率',
     'fit': '现有 AIA193 JPG 可作为适配评测对象；须复核预处理，原版本使用旧 TensorFlow', 'state': '已定位作者代码，未复现'},
    {'id': 'chronnos', 'task': 'CH', 'name': 'CHRONNOS · Jarolim et al. 2021',
     'paper': 'https://doi.org/10.1051/0004-6361/202140640',
     'code': 'https://github.com/RobertJaro/MultiChannelCHDetection', 'weights': '作者提供下载链接，尚未验证文件',
     'inputs': '原版多波段 AIA 与 HMI，FITS 标定/重投影流程',
     'fit': '当前发布 JPG 不能直接冒充原始多波段标定输入；需审计所需通道', 'state': '已定位作者代码，未复现'},
    {'id': 'deepfilament', 'task': 'FL', 'name': 'DeepFilamentSegmentation · Diercke et al. 2024',
     'paper': 'https://arxiv.org/abs/2402.15407',
     'code': 'https://github.com/adiercke/DeepFilamentSegmentation', 'weights': '待核验模型文件与依赖',
     'inputs': 'ChroTel/GONG/KSO Hα；YOLOv5 + U-Net 半监督流程',
     'fit': '与现有 Hα 任务相关；须按台站检验归一化及域迁移', 'state': '已定位作者代码，未复现'},
]


def observation_time(name):
    patterns = [r'(?:hmi\.[^.]+\.|mdi[^\d]*)(\d{8})[_\.]?(\d{6})?',
                r'AIA(\d{8})_(\d{4})', r'(?:bbso[^\d]*|.*?)(19\d{6}|20\d{6})[_\.]?(\d{6})?',
                r's?HrHa(\d{6})(\d{4}(?:\d{2})?)', r'(?<!\d)(\d{8})(?!\d)']
    for pattern in patterns:
        match = re.search(pattern, name, re.I)
        if not match:
            continue
        date = match.group(1)
        if len(date) == 6:
            date = ('19' if int(date[:2]) >= 70 else '20') + date
        try:
            day = datetime.strptime(date, '%Y%m%d').date().isoformat()
        except ValueError:
            continue
        clock = match.group(2) if match.lastindex and match.lastindex >= 2 else ''
        if clock and 'TAI' in name.upper():
            from astropy.time import Time
            raw=day+'T'+clock[:2]+':'+clock[2:4]+':'+(clock[4:6] or '00')
            converted=Time(raw,scale='tai').utc.isot
            return converted[:10],converted[11:19].replace(':','')
        return day, clock or ''
    return '', ''


def instrument_for(relative):
    name=Path(relative).name.lower()
    if name.startswith('mdi'):return 'MDI 磁图'
    if name.startswith('eit'):return 'EIT · 波段见文件名'
    parts = Path(relative).parts
    if len(parts) > 1:
        if 'H-alpha images' in parts:
            name = parts[-1].lower()
            return 'Hα · ' + ('BBSO' if 'bbso' in name else 'HSOS' if 'hrha' in name else 'KSO/其他台站')
        if parts[-2].endswith('_image'):
            return {'AR_image': 'HMI 磁图', 'CH_image': 'AIA 193 Å', 'FL_image': 'Hα'}[parts[-2]]
        return parts[-2].replace('_', ' ')
    return '数据表'


class SolarCatalog:
    def __init__(self, hub):
        self.hub, self.store = hub, hub.store

    def status(self):
        with self.store._lock:
            rows = self.store.db.execute('SELECT product,COUNT(*) n FROM solar_product GROUP BY product').fetchall()
            images = self.store.db.execute("SELECT COUNT(*) FROM solar_product WHERE category='image'").fetchone()[0]
            days = self.store.db.execute("SELECT COUNT(DISTINCT day) FROM solar_product WHERE day<>''").fetchone()[0]
            events = self.store.db.execute('SELECT COUNT(*) FROM solar_event').fetchone()[0]
            state = self.store.db.execute("SELECT payload FROM solar_catalog_state WHERE key='last_import'").fetchone()
            unknown = self.store.db.execute("SELECT COUNT(*) FROM solar_product WHERE day='' AND category='image'").fetchone()[0]
        return {'products': {r['product']: r['n'] for r in rows}, 'images': images, 'days': days,
                'events': events, 'unknown_dates': unknown, 'last_import': json.loads(state[0]) if state else None,
                'sources': [{'id': k, 'product': v, 'available': k in self.store.source_roots}
                            for k, v in SOURCE_NAMES.items()]}

    def start_index(self):
        lock = getattr(self.hub, '_solar_index_lock', None)
        if lock is None:
            lock = threading.Lock(); self.hub._solar_index_lock = lock
        with lock:
            active = getattr(self.hub, '_solar_index_task', None)
            if active and self.store.get_task(active)['state'] == 'running':
                return {'task_id': active}
            tid = self.store.create_task('solar-index', params={'sources': list(SOURCE_NAMES)})
            self.hub._solar_index_task = tid
            def run():
                try:
                    self.index(tid)
                except Exception as exc:
                    self.store.update_task(tid, 'failed', {'error': str(exc)})
            threading.Thread(target=run, daemon=True).start()
            return {'task_id': tid}

    def index(self, task_id=None):
        """Index file metadata and tables; never decode all image/mask pixels."""
        import openpyxl
        rows, events, counts = [], [], {}
        invalid_times=0
        def event_clock(hour,minute):
            nonlocal invalid_times
            try:
                h,m=int(hour),int(minute)
                if not 0<=h<24 or not 0<=m<60:raise ValueError()
                return f'{h:02}:{m:02}'
            except (ValueError,TypeError):
                invalid_times+=1
                return ''
        for source_id, product in SOURCE_NAMES.items():
            root = self.store.source_roots.get(source_id)
            if root is None:
                continue
            count = 0
            for path in sorted(root.rglob('*')):
                if not path.is_file() or path.suffix.lower() not in ('.jpg', '.npy', '.xlsx', '.txt'):
                    continue
                self.store.require_within(path)
                relative = path.relative_to(root)
                day, clock = observation_time(path.name)
                category = 'image' if path.suffix.lower() == '.jpg' else 'mask' if path.suffix.lower() == '.npy' else 'table'
                payload = {'uri': self.store.to_rel(path), 'product': product, 'day': day, 'clock': clock,
                           'instrument': instrument_for(relative), 'category': category, 'name': path.name,
                           'stem': path.stem, 'bytes': path.stat().st_size, 'relative': relative.as_posix(),
                           'original_time_standard':'TAI' if 'TAI' in path.name.upper() else 'not_explicit',
                           'time_note':'TAI已换算UTC' if 'TAI' in path.name.upper() else '按来源文件名解析；未显式声明的时间标准需核验'}
                if path.suffix.lower() == '.txt' and product == 'forecast':
                    with path.open(encoding='utf-8-sig', newline='') as stream:
                        payload['regions'] = list(csv.DictReader(stream, delimiter='\t'))
                if path.name.lower() == 'flare list.xlsx':
                    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
                    try:
                        for row in book.active.iter_rows(min_row=2, values_only=True):
                            if len(row) < 11 or row[2] is None:
                                continue
                            try:
                                event_day = datetime.strptime(str(int(row[2])), '%Y%m%d').date().isoformat()
                            except (ValueError, TypeError):
                                continue
                            event = {'day': event_day, 'start': event_clock(row[0],row[1]),
                                     'end': event_clock(row[3],row[5]),
                                     'peak': event_clock(row[7],row[8]),
                                     'class': str(row[9] or ''), 'noaa_report': str(row[10] or ''),
                                     'source_uri': payload['uri']}
                            events.append((event_day, json.dumps(event, ensure_ascii=False)))
                    finally:
                        book.close()
                rows.append((payload['uri'], product, day, payload['instrument'], category, path.stem,
                             json.dumps(payload, ensure_ascii=False)))
                count += 1
                if task_id and count % 2000 == 0:
                    self.store.update_task(task_id, 'running', {'phase': PRODUCT_NAMES[product], 'files_seen': len(rows)})
            counts[product] = count
        if not rows:
            raise ValueError('尚未登记太阳数据源，请通过 Python 启动器重新启动服务。')
        result = {'products': counts, 'files': len(rows), 'events': len(events), 'read_only': True,'invalid_event_time_fields':invalid_times,
                  'unknown_image_dates': sum(r[2] == '' and r[4] == 'image' for r in rows)}
        with self.store.transaction():
            self.store.db.execute('DELETE FROM solar_product')
            self.store.db.execute('DELETE FROM solar_event')
            self.store.db.executemany('INSERT INTO solar_product VALUES(?,?,?,?,?,?,?)', rows)
            self.store.db.executemany('INSERT INTO solar_event(day,payload) VALUES(?,?)', events)
            self.store.db.execute("INSERT OR REPLACE INTO solar_catalog_state VALUES('last_import',?)",
                                  (json.dumps(result, ensure_ascii=False),))
        if task_id:
            self.store.update_task(task_id, 'done', result)
        return result

    def days(self, search='', page=1, page_size=80):
        page, page_size = max(1, int(page)), max(1, min(100, int(page_size)))
        where = "category='image' AND day<>'' AND day LIKE ?"
        with self.store._lock:
            n = self.store.db.execute(f'SELECT COUNT(DISTINCT day) FROM solar_product WHERE {where}', (str(search)+'%',)).fetchone()[0]
            rows = self.store.db.execute(f'SELECT day,COUNT(*) n FROM solar_product WHERE {where} GROUP BY day ORDER BY day DESC LIMIT ? OFFSET ?',
                                          (str(search)+'%', page_size, (page-1)*page_size)).fetchall()
        return {'days': [dict(r) for r in rows], 'total': n, 'page': page}

    def day(self, day):
        datetime.strptime(day, '%Y-%m-%d')
        with self.store._lock:
            rows = self.store.db.execute('SELECT payload FROM solar_product WHERE day=? ORDER BY product,instrument,uri', (day,)).fetchall()
            events = self.store.db.execute('SELECT payload FROM solar_event WHERE day=? ORDER BY id', (day,)).fetchall()
        products = [json.loads(r[0]) for r in rows]
        parameters = [p for p in products if 'regions' in p]
        regions = [{**r, 'source_uri': p['uri'], 'stem': p['stem']} for p in parameters for r in p['regions']]
        return {'day': day, 'images': [p for p in products if p['category']=='image'], 'regions': regions,
                'events': [json.loads(r[0]) for r in events],
                'masks': [p for p in products if p['category']=='mask'],
                'comparison_note': '同日观测可能存在时刻、分辨率与方向差异；仅并排对照，未自动配准。'}

    def image(self, uri):
        rel = self.store.to_rel(uri)
        with self.store._lock:
            row = self.store.db.execute("SELECT uri FROM solar_product WHERE uri=? AND category='image'", (rel,)).fetchone()
        if row is None:
            raise KeyError('图像尚未登记到太阳观测索引')
        path = self.store.require_within(row['uri'])
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError('图像超过直接预览大小限制')
        return path.read_bytes()

    def region(self, uri, region_id):
        rec = self.store.indexed_item(uri)
        if not rec or rec.get('category') != 'AR' or not rec.get('mask_uri'):
            return {'available': False, 'reason': '此图没有可核验的 AR 实例参考'}
        from .solar import mask_array
        arr = mask_array(self.store, rec)
        mask = arr == int(region_id)
        if not mask.any():
            raise ValueError('活动区实例不存在')
        stem = Path(rec['uri']).stem
        with self.store._lock:
            rows = self.store.db.execute("SELECT payload FROM solar_product WHERE product='forecast' AND stem=?", (stem,)).fetchall()
        matches = [json.loads(r[0]) for r in rows]
        parameters, labels, history = None, {}, None
        for p in matches:
            if 'regions' in p:
                parameters = next((r for r in p['regions'] if str(r.get('Num')) == str(region_id)), None)
            elif p['category'] == 'mask':
                data = np.load(self.store.require_within(p['uri']), mmap_mode='r', allow_pickle=False)
                if data.shape != arr.shape:
                    continue
                values = np.unique(data[mask]).tolist()
                item = {'values': values, 'positive': bool(np.any(data[mask] > 0)), 'source_uri': p['uri']}
                if 'mask_flare_label' in p['relative']:
                    labels[Path(p['relative']).parent.name] = item
                else:
                    history = item
        return {'available': True, 'instance_id': int(region_id), 'noaa': rec.get('noaa', {}).get(str(region_id)),
                'pixels': int(mask.sum()), 'parameters': parameters, 'labels': labels, 'history': history,
                'note': '标签为数据集提供的历史观测对应结果，不是本应用实时预测概率。'}
