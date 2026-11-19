"""Host-owned trial identity and exclusive controller admission."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from revisions.dual_agent_v1_1.tools import _directory
from revisions.hybrid_agent_v1.local_numeric import _regular_bytes
from revisions.seekgnomes_v1_1_1.campaign import atomic_json

CHECKPOINT = 'controller-checkpoint.json'
CANCELLED = 'controller-cancelled.json'
_EMPTY_CHECKPOINT_TEMP = re.compile(r'controller-checkpoint\.json\.[0-9a-f]{32}\.tmp\Z')


def read_json(path):
    return json.loads(_regular_bytes(path, 128 * 1024 * 1024))


def durable_json(path, value):
    atomic_json(path, value)
    descriptor = _directory(path.parent)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def open_checkpoint(out, config, resume):
    """Create or match the immutable controller configuration before model access."""
    reopening = resume and out.exists()
    if not reopening:
        out.mkdir(mode=0o700, exist_ok=False)
    identity = {'schema': 'seekgnomes-controller-checkpoint/v1', 'configuration': config}
    path = out / CHECKPOINT
    if path.exists() or path.is_symlink():
        if not reopening or read_json(path) != identity:
            raise ValueError('Checkpoint configuration does not match this trial')
    else:
        # SIGKILL can leave the directory or an unfinished first atomic write.
        if reopening and any(not _EMPTY_CHECKPOINT_TEMP.fullmatch(p.name) or p.is_symlink()
                             for p in out.iterdir()):
            raise ValueError('Existing trial has no controller checkpoint')
        durable_json(path, identity)
    return reopening


@contextmanager
def controller_lock(out, runs):
    out = Path(os.path.abspath(out))
    if out.resolve() != out or not out.is_relative_to(runs) or out == runs:
        raise ValueError('Trial output must be a canonical path under project runs')
    directory = _directory(out.parent)
    try:
        digest = hashlib.sha256(out.name.encode()).hexdigest()[:32]
        lock = os.open('.controller-' + digest + '.lock', os.O_CREAT | os.O_RDWR |
                       os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        try:
            info = os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Unsafe controller lock')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError('This trial already has an active controller') from error
            yield out
        finally:
            os.close(lock)
    finally:
        os.close(directory)
