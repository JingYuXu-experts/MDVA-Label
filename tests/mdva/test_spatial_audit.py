import numpy as np
import pytest
from mdva.training.spatial_audit import apparent_disk


def test_apparent_disk_requires_valid_boundary_and_recovers_native_geometry():
    y,x=np.ogrid[:256,:256];disk=(x-128)**2+(y-126)**2<=119**2
    gray=np.full((256,256),255,np.uint8);gray[disk]=90
    result=apparent_disk(gray)
    assert result['cx']==pytest.approx(128,abs=.2)
    assert result['cy']==pytest.approx(126,abs=.2)
    assert result['radius']==pytest.approx(119,abs=1)
    with pytest.raises(ValueError):apparent_disk(255-gray)
    with pytest.raises(ValueError):apparent_disk(np.full((256,256),255,np.uint8))
