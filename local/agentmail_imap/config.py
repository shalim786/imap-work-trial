"""Local server defaults; client credentials are never server configuration."""
import math
import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def parse_boolean(value):
    normalized = value.strip().lower()
    if normalized not in ('true','false'):
        raise ValueError('Boolean settings must be true or false')
    return normalized == 'true'


@dataclass(frozen=True)
class Config:
    host: str = '127.0.0.1'
    port: int = 1143
    api_url: str = 'http://127.0.0.1:3210/v0'
    db_path: Path = Path('var/uids.sqlite3')
    temp_dir: Path = Path('var/tmp')
    refresh_interval: float = 60
    command_timeout: float = 300
    heartbeat_interval: float = 120
    tcp_keepalive_idle: int = 60
    tcp_keepalive_interval: int = 20
    tcp_keepalive_count: int = 3
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
    api_requests_per_second: float = 5
    download_concurrency: int = 2
    fetch_group_messages: int = 8
    metadata_group_messages: int = 256
    metadata_auth_interval: float = 60
    raw_cache_bytes: int = 64 * 1024 * 1024
    raw_cache_ttl: float = 120
    retry_initial: float = 1
    retry_max: float = 60
    write_timeout: float = 30
    assume_full_visibility: bool = False
    tls_mode: str = "local"
    tls_cert_file: Path | None = None
    tls_key_file: Path | None = None
    tls_handshake_timeout: float = 10
    tls_shutdown_timeout: float = 5

    def __post_init__(self):
        if self.tls_mode not in ('local', 'implicit', 'terminated'):
            raise ValueError('IMAP_TLS_MODE must be local, implicit, or terminated')
        if self.tls_mode == 'local':
            try: loopback = ipaddress.ip_address(self.host).is_loopback
            except ValueError: loopback = self.host.lower() == 'localhost'
            if not loopback:
                raise ValueError('Plaintext local mode requires a loopback bind address')
        if self.tls_mode == 'implicit' and not (self.tls_cert_file and self.tls_key_file):
            raise ValueError('Implicit TLS requires IMAP_TLS_CERT_FILE and IMAP_TLS_KEY_FILE')
        if self.tls_mode != 'implicit' and (self.tls_cert_file or self.tls_key_file):
            raise ValueError('Certificate files are only valid in implicit TLS mode')
        url = urlsplit(self.api_url)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('AGENTMAIL_API_URL must be an HTTP(S) URL without credentials/query/fragment')
        if not 0 <= self.port < 65536:
            raise ValueError('IMAP_PORT must be between 0 and 65535')
        if self.raw_cache_bytes < 0 or self.raw_cache_bytes > self.max_temp_bytes:
            raise ValueError('raw_cache_bytes must fit within max_temp_bytes (zero disables caching)')
        if self.retry_max < self.retry_initial:
            raise ValueError('retry_max must be at least retry_initial')
        for name in ('heartbeat_interval','tcp_keepalive_idle','tcp_keepalive_interval','tcp_keepalive_count','refresh_interval','command_timeout','idle_timeout','max_connections','max_command_bytes','max_literal_bytes','max_message_bytes','max_temp_bytes','max_batch_bytes','max_batch_messages','max_downloads','max_refreshes','write_timeout','tls_handshake_timeout','tls_shutdown_timeout','api_requests_per_second','download_concurrency','fetch_group_messages','metadata_group_messages','metadata_auth_interval','raw_cache_ttl','retry_initial','retry_max'):
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
        mapping['heartbeat_interval'] = ('IMAP_HEARTBEAT_INTERVAL_SECONDS', float)
        for name in ('tcp_keepalive_idle','tcp_keepalive_interval','tcp_keepalive_count'):
            mapping[name] = ('IMAP_' + name.upper(), int)
        mapping['assume_full_visibility'] = ('IMAP_ASSUME_FULL_VISIBILITY',parse_boolean)
        for name in ('api_requests_per_second','raw_cache_ttl','retry_initial','retry_max','metadata_auth_interval'):
            mapping[name] = ('IMAP_' + name.upper(), float)
        for name in ('download_concurrency','fetch_group_messages','metadata_group_messages','raw_cache_bytes'):
            mapping[name] = ('IMAP_' + name.upper(), int)
        mapping.update({'tls_mode':('IMAP_TLS_MODE',str),'tls_cert_file':('IMAP_TLS_CERT_FILE',Path),'tls_key_file':('IMAP_TLS_KEY_FILE',Path),'tls_handshake_timeout':('IMAP_TLS_HANDSHAKE_TIMEOUT_SECONDS',float),'tls_shutdown_timeout':('IMAP_TLS_SHUTDOWN_TIMEOUT_SECONDS',float)})
        if values.get('IMAP_TLS_MODE') == 'implicit' and 'IMAP_PORT' not in values:
            values['IMAP_PORT'] = '1993'
        for name in ('max_connections','max_command_bytes','max_literal_bytes','max_message_bytes','max_temp_bytes','max_batch_bytes','max_batch_messages','max_downloads','max_refreshes'):
            mapping[name] = ('IMAP_' + name.upper(), int)
        return cls(**{name: cast(values[key]) for name,(key,cast) in mapping.items() if key in values})
