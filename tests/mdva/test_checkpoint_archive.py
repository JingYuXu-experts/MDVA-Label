import hashlib

import pytest

from mdva.training import checkpoint_archive as archive


def test_archive_preserves_bytes_and_survives_training_overwrite(tmp_path):
    source, target = tmp_path/'best.pt', tmp_path/'review'/'checkpoint.pt'
    source.write_bytes(b'epoch13 verified weights')
    sha = archive.digest(source)
    archive.archive_checkpoint(source, target, sha)
    archive.archive_checkpoint(source, target, sha)  # Verified resume is idempotent.
    source.write_bytes(b'epoch18 new weights')
    assert target.read_bytes() == b'epoch13 verified weights'
    assert archive.digest(target) == sha


def test_conflicting_or_wrong_source_is_not_overwritten(tmp_path):
    source, target = tmp_path/'best.pt', tmp_path/'checkpoint.pt'
    source.write_bytes(b'new'); target.write_bytes(b'existing')
    with pytest.raises(ValueError, match='冲突'):
        archive.archive_checkpoint(source, target, archive.digest(source))
    assert target.read_bytes() == b'existing'
    with pytest.raises(ValueError, match='身份不符'):
        archive.archive_checkpoint(source, tmp_path/'other.pt', '0'*64)
    assert not (tmp_path/'other.pt').exists()


def test_source_mutation_during_copy_cannot_publish_archive(tmp_path, monkeypatch):
    source, target = tmp_path/'best.pt', tmp_path/'checkpoint.pt'
    source.write_bytes(b'first')
    sha = archive.digest(source)
    calls = 0
    original_digest = archive.digest

    def changing_digest(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            source.write_bytes(b'changed')
        return original_digest(path)

    monkeypatch.setattr(archive, 'digest', changing_digest)
    with pytest.raises(ValueError, match='复制期间'):
        archive.archive_checkpoint(source, target, sha)
    assert not target.exists()
    assert not list(tmp_path.glob('.*.tmp'))


def test_archive_cannot_alias_source(tmp_path):
    source = tmp_path/'best.pt'; source.write_bytes(b'weights')
    with pytest.raises(ValueError, match='独立'):
        archive.archive_checkpoint(source, source, hashlib.sha256(b'weights').hexdigest())
