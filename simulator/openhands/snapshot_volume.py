"""Stream host snapshots into Docker-managed storage, never a shared bind view."""
import subprocess
import tarfile
import tempfile
from pathlib import Path


INSTALL = '''
import os, pathlib, shutil, sys, tarfile
root = pathlib.Path('/snapshot')
for current, dirs, files in os.walk(root):
    os.chmod(current, 0o755)
for child in root.iterdir():
    if child.is_dir() and not child.is_symlink():
        shutil.rmtree(child)
    else:
        child.unlink()
def checked(member, destination):
    safe = tarfile.data_filter(member, destination)
    safe.mode = member.mode
    return safe
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
    archive.extractall(root, filter=checked)
'''


def write_archive(root, stream):
    """Archive only ordinary entries, preserving the candidate fingerprint."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('snapshot root must be a directory')
    with tarfile.open(fileobj=stream, mode='w') as archive:
        for path in sorted(root.rglob('*')):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError('unsafe snapshot entry')
            item = archive.gettarinfo(str(path), arcname=path.relative_to(root).as_posix())
            if item.mode & 0o6000:
                raise ValueError('set-id snapshot entries are not supported')
            item.uid = item.gid = 0
            item.uname = item.gname = ''
            if path.is_file():
                with path.open('rb') as content:
                    archive.addfile(item, content)
            else:
                archive.addfile(item)


def upload_snapshot(root, volume, image):
    """Replace a private volume while its reader is frozen; failure is fatal."""
    with tempfile.TemporaryFile() as stream:
        write_archive(root, stream)
        stream.seek(0)
        subprocess.run([
            'docker', 'run', '--rm', '-i', '--network', 'none',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--mount', f'type=volume,src={volume},dst=/snapshot,volume-nocopy',
            '--entrypoint', 'python', image, '-c', INSTALL,
        ], stdin=stream, capture_output=True, check=True, timeout=120)
