from mdva.training.fl_candidate_review import FEATURES, apply_review
from mdva.training.fl_review_agreement import review


def test_disagreeing_reference_candidate_remains_available_for_review():
    confidence_only = [0.] * len(FEATURES)
    confidence_only[5] = .85
    rule = {'classifier': {'mean': [0.] * len(FEATURES), 'scale': [1.] * len(FEATURES),
                           'weights': [1.] + [0.] * (len(FEATURES) - 1), 'intercept': 0.},
            'cutoffs': {'probability': .7, 'shape_and_brightness': .3}}
    data = {'name': 'sample', 'time': '2020-01-01', 'station': 'Bh',
            'features': [confidence_only],
            'geometry': [{'reference_ids': [1], 'reference_sizes': [25], 'iou': [[.62]]}]}
    previous = apply_review(data, rule['classifier'], rule['cutoffs'])

    report = review(previous, rule)

    assert report['results']['either_signal'][0]['matched'] == 1
    assert report['results']['both_signals'][0]['matched'] == 0
    assert report['triage']['confidence_only']['matched_by_any_annotator'] == 1
