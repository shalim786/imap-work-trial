"""Content-free metadata used by session snapshots."""
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Message:
    message_id: str
    uid: int = 0
    labels: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    size: int = 0

    @property
    def flags(self) -> list[str]:
        # Unread wins if upstream labels contradict each other.
        return ([r'\Seen'] if 'read' in self.labels and 'unread' not in self.labels else []) + ([r'\Flagged'] if 'starred' in self.labels else [])
