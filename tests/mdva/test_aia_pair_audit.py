import pytest
from mdva.training.aia_pair_audit import eligible_training


def test_source_audit_cannot_include_calibration_test_or_duplicate_inputs():
    rows=[{'uri':str(i),'category':'CH','split':'train'} for i in range(16)];uris=[r['uri'] for r in rows]
    assert len(eligible_training(uris,rows))==16
    for split in ('validation','calibration','test'):
        altered=[dict(r) for r in rows];altered[0]['split']=split
        with pytest.raises(ValueError):eligible_training(uris,altered)
    with pytest.raises(ValueError):eligible_training(uris[:-1]+[uris[0]],rows)
