from datetime import date

import pytest

from mdva.training.fl_shape_fresh import select


def frame(stamp, observed, identity):
    name = stamp + 'Bh'
    return {'file_name': name + '.jpeg', 'date_captured': observed, 'id': identity,
            'url': f'https://gong2.nso.edu/HA/hag/{stamp[:6]}/{stamp[:8]}/{name}.jpg',
            'height': 2048, 'width': 2048}


def test_fresh_selection_groups_same_frame_and_excludes_known_dates():
    images = [frame('20190322144450', '2019-03-22 14:44:50', 'a'),
              frame('20190322144450', '2019-03-22 14:44:50', 'b'),
              frame('20190101144450', '2019-01-01 14:44:50', 'c'),
              frame('20230101144450', '2023-01-01 14:44:50', 'd')]
    selected, _ = select({'images': images, 'annotations': []}, {date(2019, 1, 1)})
    assert len(selected) == 1
    assert selected[0]['variants'] == ['a', 'b']
    assert selected[0]['site'] == 'Bh'


def test_fresh_selection_rejects_metadata_timestamp_disagreement():
    image = frame('20190322144450', '2019-03-22 15:44:50', 'a')
    with pytest.raises(ValueError):
        select({'images': [image], 'annotations': []}, set())
