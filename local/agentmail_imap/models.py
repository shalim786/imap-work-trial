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
    def in_inbox(self) -> bool:
        return 'received' in self.labels and 'trash' not in self.labels

    @property
    def flags(self) -> list[str]:
        # AgentMail represents read messages by the absence of unread.
        return ([r'\Seen'] if 'unread' not in self.labels else []) + ([r'\Flagged'] if 'starred' in self.labels else [])
