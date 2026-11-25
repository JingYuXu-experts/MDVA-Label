import pytest
from mdva.training.fl_validation_review import validation_data

@pytest.mark.parametrize('split',['train','test','calibration','unknown'])
def test_unapproved_splits_rejected_before_reading_files(tmp_path,split):
    with pytest.raises(ValueError,match='只允许开发验证'):validation_data(tmp_path/'absent',split)
