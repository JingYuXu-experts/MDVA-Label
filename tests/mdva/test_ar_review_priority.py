import pytest

pytest.importorskip('torch')

from mdva.training.ar_review_priority import review_curve


def test_review_order_counts_flags_and_keeps_every_candidate():
    candidates = [
        {'sample_id': f'obs-{i // 2}', 'component': i % 2 + 1,
         'score': -i, 'matched_reference': i < 6}
        for i in range(10)
    ]
    curve, ordered = review_curve(candidates, 'score')
    budget = curve[1]
    assert len(ordered) == len(candidates) == 10
    assert budget['reviewed_candidates'] == 2
    assert budget['images_to_open'] == 1
    assert budget['matched_flagged'] == 0
    assert budget['unmatched_flagged_share'] == .5
    assert candidates[0]['score'] == 0
