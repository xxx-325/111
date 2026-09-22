import io
import tarfile
from unittest.mock import MagicMock, patch

import pytest

from simulator.openhands.snapshot_volume import write_archive
from simulator.openhands.sandbox import ExecutionSandbox


def test_archive_preserves_lengths_empty_directories_and_permissions(tmp_path):
    (tmp_path / 'empty').mkdir()
    path = tmp_path / 'example.py'
    path.write_text('assert True\n')
    path.chmod(0o777)
    for text in ('assert True\n', 'assert 123456789 == 123456789\n', 'x=0\n'):
        path.write_text(text)
        buffer = io.BytesIO()
        write_archive(tmp_path, buffer)
        buffer.seek(0)
        with tarfile.open(fileobj=buffer) as archive:
            assert archive.extractfile('example.py').read() == text.encode()
            assert archive.getmember('example.py').mode == 0o777
            assert archive.getmember('empty').isdir()


def test_archive_rejects_link_and_setid(tmp_path):
    path = tmp_path / 'entry'
    path.symlink_to('/etc/passwd')
    with pytest.raises(ValueError, match='unsafe snapshot'):
        write_archive(tmp_path, io.BytesIO())
    path.unlink()
    path.write_text('data')
    path.chmod(0o4755)
    with pytest.raises(ValueError, match='set-id'):
        write_archive(tmp_path, io.BytesIO())


def test_failed_delivery_keeps_reader_frozen():
    sandbox = ExecutionSandbox.__new__(ExecutionSandbox)
    sandbox.role = 'judge'
    sandbox.verify = MagicMock()
    sandbox.pause = MagicMock()
    sandbox.unpause = MagicMock()
    sandbox.mounts = [('source', '/workspace/candidate', True)]
    sandbox.volumes = {'/workspace/candidate': 'private-volume'}
    sandbox.image = 'pinned'
    with patch('simulator.openhands.sandbox.upload_snapshot', side_effect=RuntimeError('transfer failed')):
        with pytest.raises(RuntimeError, match='transfer failed'):
            sandbox.sync_snapshots()
    sandbox.pause.assert_called_once()
    sandbox.unpause.assert_not_called()
