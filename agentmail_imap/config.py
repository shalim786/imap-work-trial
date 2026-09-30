"""Local server defaults; client credentials are never server configuration."""
import math
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    host: str = '127.0.0.1'
    port: int = 1143
    api_url: str = 'http://127.0.0.1:3210/v0'
    db_path: Path = Path('var/uids.sqlite3')
    temp_dir: Path = Path('var/tmp')
    refresh_interval: float = 60
    command_timeout: float = 300
    idle_timeout: float = 1800
    max_connections: int = 100
    max_command_bytes: int = 65536
    max_literal_bytes: int = 65536
    max_message_bytes: int = 25 * 1024 * 1024
    max_temp_bytes: int = 256 * 1024 * 1024
    max_batch_bytes: int = 100 * 1024 * 1024
    max_batch_messages: int = 1000
    max_downloads: int = 4
    max_refreshes: int = 4
    write_timeout: float = 30

    def __post_init__(self):
        url = urlsplit(self.api_url)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('AGENTMAIL_API_URL must be an HTTP(S) URL without credentials/query/fragment')
        if not 0 <= self.port < 65536:
            raise ValueError('IMAP_PORT must be between 0 and 65535')
        for name in ('refresh_interval','command_timeout','idle_timeout','max_connections','max_command_bytes','max_literal_bytes','max_message_bytes','max_temp_bytes','max_batch_bytes','max_batch_messages','max_downloads','max_refreshes','write_timeout'):
            if not math.isfinite(getattr(self,name)) or getattr(self, name) <= 0:
                raise ValueError(f'{name} must be positive')

    @classmethod
    def from_env(cls, env_file: str | Path = '.env'):
        values = {}
        path = Path(env_file)
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith('#'): continue
                if line.startswith('export '): line = line[7:]
                if '=' not in line: raise ValueError('Invalid .env assignment')
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip().strip('\"\'')
        values.update(os.environ)
        if 'IMAP_UID_DB' in values: values['IMAP_DB_PATH'] = values['IMAP_UID_DB']
        mapping = {'host':('IMAP_HOST',str),'port':('IMAP_PORT',int),'api_url':('AGENTMAIL_API_URL',str),'db_path':('IMAP_DB_PATH',Path),'temp_dir':('IMAP_TEMP_DIR',Path),'refresh_interval':('IMAP_REFRESH_INTERVAL_SECONDS',float),'command_timeout':('IMAP_COMMAND_TIMEOUT_SECONDS',float),'idle_timeout':('IMAP_IDLE_TIMEOUT_SECONDS',float),'write_timeout':('IMAP_WRITE_TIMEOUT_SECONDS',float)}
        for name in ('max_connections','max_command_bytes','max_literal_bytes','max_message_bytes','max_temp_bytes','max_batch_bytes','max_batch_messages','max_downloads','max_refreshes'):
            mapping[name] = ('IMAP_' + name.upper(), int)
        return cls(**{name: cast(values[key]) for name,(key,cast) in mapping.items() if key in values})
