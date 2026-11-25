import numpy as np
import pytest

from mdva.training.fl_magfilo_audit import compare, group_observations, raster


def test_magfilo_shares_one_observation_without_merging_two_independent_labels():
    baseline = {
        'url': 'https://gong2.nso.edu/HA/hag/201708/20170815/20170815120203Bh.jpg',
        'date_captured': '2017-08-15 12:02:03', 'width': 2048, 'height': 2048,
    }
    images = [{**baseline, 'id': 'a'}, {**baseline, 'id': 'b'},
              {**baseline, 'id': 'future', 'date_captured': '2019-08-15 12:02:03'}]
    items = [{'image_id': image['id']} for image in images]

    returned, annotations = group_observations({'images': images, 'annotations': items})

    assert list(returned) == [baseline['url']]
    assert [image['id'] for image in returned[baseline['url']]] == ['a', 'b']
    assert set(annotations) == {'a', 'b'}


def test_magfilo_raster_keeps_xy_order_and_matches_identical_shape():
    ring = [400, 80, 460, 80, 460, 112, 400, 112]
    mask = raster([{'iscrowd': 0, 'segmentation': [ring]}])

    assert mask.shape == (512, 512) and mask[22, 103] and not mask[103, 22]
    assert compare(mask, mask)['matched_iou_050'] == 1
    empty = np.zeros_like(mask)
    assert compare(mask, empty)['dice'] == compare(empty, mask)['dice'] == 0
    assert compare(empty, empty)['dice'] is None
    with pytest.raises(ValueError, match='超界'):
        raster([{'iscrowd': 0, 'segmentation': [[np.nan, *ring[1:]]]}])
