"""Read-only Gmail IMAP adapter using TLS, EXAMINE, and BODY.PEEK."""
from __future__ import annotations

import imaplib
import re
import ssl
from typing import Any, Callable

from gmail_provider import MAX_RAW_MESSAGE_BYTES


class GmailIMAPProvider:
    """Fetch a bounded snapshot of INBOX without changing message flags."""

    name = 'gmail-imap'

    def __init__(self, username: str, password: str, page_size: int = 50,
                 initial_window_uids: int = 1000, timeout: float = 20,
                 client_factory: Callable[..., Any] | None = None):
        if not username or not password:
            raise ValueError('Gmail IMAP username and app password are required')
        if not 1 <= page_size <= 500:
            raise ValueError('IMAP page size must be between 1 and 500')
        if not 1 <= initial_window_uids <= 10000:
            raise ValueError('Initial IMAP window must be between 1 and 10000 UIDs')
        if timeout <= 0 or timeout > 120:
            raise ValueError('IMAP timeout must be between 0 and 120 seconds')
        self.username = username
        # Google displays generated app passwords in groups; spaces are presentation only.
        self._password = ''.join(password.split())
        self.page_size = page_size
        self.initial_window_uids = initial_window_uids
        self.timeout = timeout
        self.client_factory = client_factory or self._connect
        self.skipped_oversize = 0

    @staticmethod
    def _connect(host: str, port: int, timeout: float):
        return imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)

    @staticmethod
    def _ok(result, operation: str):
        status, data = result
        if status != 'OK':
            raise RuntimeError(f'Gmail IMAP {operation} failed')
        return data

    @staticmethod
    def _checkpoint(value: str | None):
        if value is None:
            return None
        match = re.fullmatch(r'([0-9]+):([0-9]+):([0-9]+)', value)
        if not match:
            raise ValueError('Saved Gmail IMAP checkpoint is invalid; clear its sync state and retry')
        return tuple(map(int, match.groups()))

    def _snapshot(self, client):
        data = self._ok(client.status('INBOX', '(UIDNEXT UIDVALIDITY)'), 'mailbox status')
        text = b' '.join(part for part in data if isinstance(part, bytes)).decode('ascii', 'ignore')
        uidnext = re.search(r'UIDNEXT\s+(\d+)', text)
        validity = re.search(r'UIDVALIDITY\s+(\d+)', text)
        if not uidnext or not validity:
            raise RuntimeError('Gmail IMAP did not provide mailbox synchronization identifiers')
        return int(validity.group(1)), max(0, int(uidnext.group(1)) - 1)

    def fetch_page(self, checkpoint: str | None):
        """Return one stable UID snapshot page; cursor survives process restarts."""
        client = None
        try:
            client = self.client_factory('imap.gmail.com', 993, self.timeout)
            self._ok(client.login(self.username, self._password), 'authentication')
            self._ok(client.select('INBOX', readonly=True), 'read-only mailbox selection')
            validity, current_end = self._snapshot(client)
            saved = self._checkpoint(checkpoint)
            if saved is None or saved[0] != validity:
                end = current_end
                start = max(1, end - self.initial_window_uids + 1)
                last = start - 1
            else:
                old_validity, last, old_end = saved
                if last >= old_end:
                    # The previous snapshot completed. Begin an incremental range.
                    start, end = last + 1, current_end
                else:
                    # Resume the same bounded snapshot after an interrupted run.
                    start, end = last + 1, old_end
                end = min(end, current_end)

            if start > end:
                cursor = f'{validity}:{end}:{end}'
                return [], cursor, False

            search_data = self._ok(client.uid('SEARCH', None, 'UID', f'{start}:{end}'), 'UID search')
            uid_bytes = next((part for part in search_data if isinstance(part, bytes)), b'')
            uids = [int(item) for item in uid_bytes.split() if item.isdigit() and int(item) > last]
            page_uids = uids[:self.page_size]
            raw_messages = []
            for uid in page_uids:
                size_data = self._ok(client.uid('FETCH', str(uid), '(RFC822.SIZE)'), 'message size fetch')
                size_text = b' '.join(x for x in size_data if isinstance(x, bytes))
                size_match = re.search(rb'RFC822\.SIZE\s+(\d+)', size_text)
                if not size_match:
                    raise RuntimeError('Gmail IMAP did not return a message size')
                if int(size_match.group(1)) > MAX_RAW_MESSAGE_BYTES:
                    self.skipped_oversize += 1
                    continue
                body_data = self._ok(client.uid('FETCH', str(uid), '(BODY.PEEK[])'), 'read-only message fetch')
                raw = next((part[1] for part in body_data if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], bytes)), None)
                if raw is None or len(raw) > MAX_RAW_MESSAGE_BYTES:
                    self.skipped_oversize += 1
                    continue
                raw_messages.append(raw)

            new_last = page_uids[-1] if page_uids else end
            more = new_last < end
            cursor = f'{validity}:{new_last}:{end}'
            return raw_messages, cursor, more
        except (imaplib.IMAP4.error, OSError, ssl.SSLError, RuntimeError, ValueError):
            raise RuntimeError('Gmail IMAP sync failed. Check the account, app password, network, and IMAP access.') from None
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass
