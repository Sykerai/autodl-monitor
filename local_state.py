"""Private atomic local files and one active process per configuration directory."""
import json
import os
import tempfile
from pathlib import Path
from contextlib import contextmanager


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as handle:
            temporary = handle.name
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            os.unlink(temporary)


def read_json(path, default=None):
    try:
        with Path(path).open(encoding='utf-8') as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f'本地记录 {Path(path).name} 损坏，请保留文件并检查，不能自动重新下单') from exc


@contextmanager
def process_lock(directory):
    import fcntl
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    with (directory / 'process.lock').open('a') as handle:
        os.chmod(handle.name, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('已有一个 autodl-monitor 正在运行，请先结束它再启动') from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
