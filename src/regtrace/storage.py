import hashlib
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
import psycopg
from psycopg.rows import dict_row

@contextmanager
def transaction(dsn, tenant=None):
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        if tenant is not None:
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant,))
        yield conn

class ArtifactStore:
    """Content-addressed immutable blobs; rollback may leave unreferenced blobs."""
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, digest):
        if not re.fullmatch('[a-f0-9]{64}', digest):
            raise ValueError('Invalid artifact digest')
        return self.root/digest[:2]/digest

    def put(self, content, expected):
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError('Artifact digest mismatch')
        target=self.path(expected); target.parent.mkdir(parents=True,exist_ok=True)
        fd, temporary=tempfile.mkstemp(dir=target.parent)
        try:
            with os.fdopen(fd,'wb') as handle:
                handle.write(content); handle.flush(); os.fsync(handle.fileno())
            os.chmod(temporary,0o444)
            try: os.link(temporary,target)
            except FileExistsError: pass
            directory=os.open(target.parent,os.O_RDONLY | os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
            self.read(expected)
        finally: os.unlink(temporary)
        return expected

    def read(self, digest):
        data=self.path(digest).read_bytes()
        if hashlib.sha256(data).hexdigest()!=digest:
            raise ValueError('Stored artifact corrupted')
        return data
