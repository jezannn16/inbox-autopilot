"""Read-only Gmail API adapter and local desktop OAuth credential handling."""
from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path
from typing import Any

GMAIL_READONLY_SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
MAX_RAW_MESSAGE_BYTES = 10_000_000


class GmailProvider:
    """Fetch Gmail RFC 822 messages without changing mailbox state."""

    name = 'gmail'

    def __init__(self, service: Any, query: str = 'in:inbox', page_size: int = 100):
        self.service = service
        self.query = query
        self.page_size = page_size

    def fetch_page(self, checkpoint: str | None):
        params = {'userId': 'me', 'q': self.query, 'maxResults': self.page_size}
        if checkpoint:
            params['pageToken'] = checkpoint
        page = self.service.users().messages().list(**params).execute()
        raw_messages = []
        for item in page.get('messages', []):
            message = self.service.users().messages().get(
                userId='me', id=item['id'], format='raw'
            ).execute()
            raw = base64.urlsafe_b64decode(message['raw'] + '=' * (-len(message['raw']) % 4))
            if len(raw) > MAX_RAW_MESSAGE_BYTES:
                raise ValueError('A Gmail message exceeds the 10 MB import limit; no data was stored for it')
            raw_messages.append(raw)
        next_checkpoint = page.get('nextPageToken')
        return raw_messages, next_checkpoint, bool(next_checkpoint)


def _secure_token_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise ValueError('The token directory must not be a symbolic link')
    try:
        path.parent.chmod(0o700)
    except OSError:
        raise ValueError('Could not restrict permissions on the token directory') from None
    if path.is_symlink():
        raise ValueError('The token file must not be a symbolic link')


def _save_token(path: Path, serialized: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix='.gmail-token-', dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as token_file:
            token_file.write(serialized)
            token_file.flush()
            os.fsync(token_file.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def authorize_gmail(credentials_path: Path, token_path: Path):
    """Load or create a desktop OAuth grant and build the Gmail API service."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    credentials_path = Path(credentials_path).expanduser()
    token_path = Path(token_path).expanduser()
    if credentials_path.is_symlink():
        raise ValueError('The OAuth client file must not be a symbolic link')
    if not credentials_path.is_file():
        raise FileNotFoundError(f'OAuth client JSON not found: {credentials_path}')
    _secure_token_parent(token_path)

    credentials = None
    if token_path.exists():
        try:
            token_path.chmod(0o600)
            credentials = Credentials.from_authorized_user_file(str(token_path), [GMAIL_READONLY_SCOPE])
        except (OSError, ValueError):
            raise ValueError('The saved Gmail token is unreadable; remove it and authorize again') from None

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except Exception:
            raise RuntimeError('The saved Gmail grant could not be refreshed; remove the token file and authorize again') from None
    elif not credentials or not credentials.valid:
        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), [GMAIL_READONLY_SCOPE]
            )
            credentials = flow.run_local_server(port=0, access_type='offline')
        except Exception:
            raise RuntimeError('Gmail desktop authorization failed; check the OAuth client setup and try again') from None

    _save_token(token_path, credentials.to_json())
    try:
        return build('gmail', 'v1', credentials=credentials, cache_discovery=False)
    except Exception:
        raise RuntimeError('Could not initialize the Gmail API client') from None
