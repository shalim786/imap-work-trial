"""Private per-run raw storage, with narrowly scoped stale-run cleanup."""
import contextlib
import os
import shutil
import tempfile
from pathlib import Path

@contextlib.contextmanager
def run_directory(base):
    base = Path(base)
    base.mkdir(parents=True,exist_ok=True)
    for candidate in base.glob('run-*'):
        marker = candidate / 'owner.pid'
        if candidate.is_symlink() or not marker.is_file():
            continue
        try:
            pid = int(marker.read_text())
            if pid <= 0: continue
            os.kill(pid,0)
        except ProcessLookupError:
            shutil.rmtree(candidate)
        except (ValueError,OSError):
            continue
    with tempfile.TemporaryDirectory(prefix='run-',dir=base) as directory:
        os.chmod(directory,0o700)
        (Path(directory)/'owner.pid').write_text(str(os.getpid()))
        yield Path(directory)
