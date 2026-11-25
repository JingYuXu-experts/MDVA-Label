import pytest

from mdva.training.fl_magfilo_transfer import observation_name, verify_inventory


def observation():
    return {
        'id': 'a', 'file_name': '20170315140218Bh.jpeg', 'width': 2048, 'height': 2048,
        'date_captured': '2017-03-15 14:02:18',
        'url': 'https://gong2.nso.edu/HA/hag/201703/20170315/20170315140218Bh.jpg',
    }


def test_transfer_joins_record_by_image_and_exact_annotation_geometry():
    image = observation()
    shape = {
        'id': 'polygon', 'image_id': image['id'], 'category_id': 3, 'iscrowd': 0,
        'bbox': [10, 10, 30, 30], 'segmentation': [[10, 10, 40, 10, 40, 40]],
    }
    ref = {'images': [image], 'annotations': [shape]}
    names = {image['file_name']}
    rows, _ = verify_inventory(ref, ref, names)

    assert len(rows) == 1 and rows[0]['role'] == 'transfer_dev'
    changed = {**shape, 'segmentation': [[11, 10, 40, 10, 40, 40]]}
    assert verify_inventory(ref, {'images': [image], 'annotations': [changed]}, names)[0] == []
    assert verify_inventory(ref, ref, set())[0] == []


def test_transfer_refuses_a_mismatched_timestamp_or_path():
    image = observation()
    assert observation_name(image) == image['file_name']
    for filename in ('20170315140219Bh.jpeg', '../20170315140218Bh.jpeg'):
        with pytest.raises(ValueError):
            observation_name({**image, 'file_name': filename})
