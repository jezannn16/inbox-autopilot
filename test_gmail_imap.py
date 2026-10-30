import tempfile
import unittest
from pathlib import Path

import app
from gmail_provider import MAX_RAW_MESSAGE_BYTES
from gmail_imap_provider import GmailIMAPProvider


def eml(uid, message_id=None):
    return (f'From: person{uid}@example.test\r\nSubject: Message {uid}\r\n'
            f'Message-ID: <{message_id or uid}@example.test>\r\n\r\nTODO: Review item {uid}').encode()


class FakeIMAP:
    def __init__(self, state):
        self.state = state
        self.calls = []

    def login(self, username, password):
        self.calls.append(('login', username, password)); return 'OK', [b'authenticated']

    def select(self, mailbox, readonly=False):
        self.calls.append(('select', mailbox, readonly)); return 'OK', [b'4']

    def status(self, mailbox, items):
        self.calls.append(('status', mailbox, items))
        return 'OK', [f'INBOX (UIDNEXT {self.state["uidnext"]} UIDVALIDITY {self.state["validity"]})'.encode()]

    def uid(self, command, *args):
        self.calls.append(('uid', command, *args))
        if command == 'SEARCH':
            start, end = map(int, args[-1].split(':'))
            return 'OK', [' '.join(str(uid) for uid in self.state['messages'] if start <= uid <= end).encode()]
        uid = int(args[0])
        raw = self.state['messages'][uid]
        if args[1] == '(RFC822.SIZE)':
            size = self.state.get('sizes', {}).get(uid, len(raw))
            return 'OK', [f'{uid} (RFC822.SIZE {size})'.encode()]
        if args[1] == '(BODY.PEEK[])':
            return 'OK', [(f'{uid} (BODY[] {{{len(raw)}}}'.encode(), raw)]
        raise AssertionError(f'Unexpected IMAP fetch: {args!r}')

    def logout(self):
        self.calls.append(('logout',)); return 'BYE', [b'logged out']


class GmailIMAPTests(unittest.TestCase):
    def provider(self, state, clients, **kwargs):
        def factory(host, port, timeout):
            clients.append((host, port, timeout))
            client = FakeIMAP(state); state.setdefault('calls', []).append(client.calls)
            return client
        return GmailIMAPProvider('synthetic@gmail.test', 'do-not-log-this',
                                 client_factory=factory, **kwargs)

    def test_readonly_sync_pages_restarts_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as td:
            state = {'uidnext': 5, 'validity': 71, 'messages': {i: eml(i) for i in range(1, 5)}}
            clients = []; provider = self.provider(state, clients, page_size=2, initial_window_uids=10)
            db = app.connect(Path(td) / 'mail.db')
            with self.assertRaisesRegex(ValueError, 'checkpoint was saved'):
                app.sync_readonly(provider, db, max_pages=1)
            self.assertEqual(db.execute('SELECT checkpoint FROM sync_state').fetchone()[0], '71:2:4')
            self.assertEqual(app.sync_readonly(provider, db), 2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 4)
            checkpoint = db.execute('SELECT checkpoint FROM sync_state WHERE provider=?', ('gmail-imap',)).fetchone()[0]
            self.assertEqual(checkpoint, '71:4:4')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages WHERE body LIKE '%do-not-log-this%'").fetchone()[0], 0)
            # A new UID with a previously imported Message-ID remains deduplicated after restart.
            state['uidnext'] = 6; state['messages'][5] = eml(5, message_id='4')
            self.assertEqual(app.sync_readonly(self.provider(state, clients, page_size=2), db), 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 4)
            self.assertEqual(len(clients), 3)
            for client_calls in state['calls']:
                self.assertIn(('select', 'INBOX', True), client_calls)
                fetch_calls = [call for call in client_calls if call[0] == 'uid' and call[1] == 'FETCH']
                body_calls = [call for call in fetch_calls if '(BODY.PEEK[])' in call]
                self.assertTrue(body_calls)
                self.assertTrue(all(call[3] in ('(RFC822.SIZE)', '(BODY.PEEK[])') for call in fetch_calls))
                self.assertFalse(any(call[0] == 'store' or call[0] == 'copy' or call[0] == 'expunge' for call in client_calls))
            self.assertTrue(all((host, port, timeout) == ('imap.gmail.com', 993, 20) for host, port, timeout in clients))
            db.close()

    def test_missing_credentials_fails_before_network(self):
        with tempfile.TemporaryDirectory() as td:
            db = app.connect(Path(td) / 'mail.db')
            with self.assertRaisesRegex(ValueError, 'GMAIL_IMAP_USERNAME'):
                app.sync_gmail_imap(db, username='', password='')
            db.close()

    def test_oversize_message_advances_cursor_without_fetching_body(self):
        with tempfile.TemporaryDirectory() as td:
            state = {'uidnext': 2, 'validity': 9, 'messages': {1: eml(1)}, 'sizes': {1: MAX_RAW_MESSAGE_BYTES + 1}}
            clients = []; provider = self.provider(state, clients, page_size=5)
            db = app.connect(Path(td) / 'mail.db')
            self.assertEqual(app.sync_readonly(provider, db), 0)
            self.assertEqual(provider.skipped_oversize, 1)
            self.assertEqual(db.execute('SELECT checkpoint FROM sync_state').fetchone()[0], '9:1:1')
            fetches = [call for calls in state['calls'] for call in calls if call[0] == 'uid' and call[1] == 'FETCH']
            self.assertEqual(len(fetches), 1)
            self.assertEqual(fetches[0][3], '(RFC822.SIZE)')
            db.close()

    def test_auth_failure_is_redacted_and_timeout_is_bounded(self):
        class BadAuth(FakeIMAP):
            def login(self, username, password):
                raise RuntimeError(f'password was {password}')
        provider = GmailIMAPProvider('synthetic@gmail.test', 'secret', client_factory=lambda *a: BadAuth({}))
        with self.assertRaisesRegex(RuntimeError, 'Check the account') as caught:
            provider.fetch_page(None)
        self.assertNotIn('secret', str(caught.exception))
        timeout = GmailIMAPProvider('u', 'p', client_factory=lambda *a: (_ for _ in ()).throw(TimeoutError()))
        with self.assertRaisesRegex(RuntimeError, 'sync failed'):
            timeout.fetch_page(None)


if __name__ == '__main__':
    unittest.main()
