import json
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

@dataclass(frozen=True)
class Settings:
    app_dsn: str
    auth_dsn: str
    artifact_root: Path
    origin: str = 'http://127.0.0.1:8000'
    secure_cookie: bool = False
    lease_seconds: int = 30
    tenants: tuple[str, ...] = ('demo-bank', 'other-bank')

    @classmethod
    def load(cls):
        data = json.loads(Path(os.environ.get('REGTRACE_CONFIG', ROOT/'.local/runtime.json')).read_text())
        return cls(app_dsn=data['app_dsn'], auth_dsn=data['auth_dsn'],
                   artifact_root=Path(data['artifact_root']), origin=data['origin'],
                   secure_cookie=data.get('secure_cookie', False),
                   tenants=tuple(data.get('tenants', ['demo-bank','other-bank'])))
