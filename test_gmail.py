import base64
import os
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import app
from gmail_provider import GMAIL_READONLY_SCOPE, GmailProvider, authorize_gmail


class FakeRequest:
    def __init__(self, service, method, **params):
        self.service, self.method, self.params = service, method, params

    def execute(self):
        self.service.calls.append((self.method, self.params))
        if self.method == 'list':
            if self.params.get('pageToken') == 'page-2':
                return {'messages': [{'id': 'second'}]}
            return {'messages': [{'id': 'first'}], 'nextPageToken': 'page-2'}
        raw = self.service.raw[self.params['id']]
        return {'raw': base64.urlsafe_b64encode(raw).decode().rstrip('=')}


class FakeMessages:
    def __init__(self, service):
        self.service = service

    def list(self, **params):
        return FakeRequest(self.service, 'list', **params)

    def get(self, **params):
        return FakeRequest(self.service, 'get', **params)


class FakeUsers:
    def __init__(self, service):
        self.service = service

    def messages(self):
        return FakeMessages(self.service)


class FakeGmailService:
    def __init__(self):
        self.calls = []
        self.raw = {
            'first': b'From: one@example.test\nSubject: First\nMessage-ID: <first>\n\nTODO: Review the brief',
            'second': b'From: two@example.test\nSubject: Second\nMessage-ID: <second>\n\nA second synthetic message',
        }

    def users(self):
        return FakeUsers(self)


class GmailTests(unittest.TestCase):
    def test_readonly_provider_paginates_into_the_local_mail_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            db = app.connect(Path(directory) / 'mail.db')
            service = FakeGmailService()
            provider = GmailProvider(service, query='in:inbox newer_than:7d')

            self.assertEqual(app.sync_readonly(provider, db), 2)
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM action_items').fetchone()[0], 0)
            first = db.execute('SELECT id FROM messages WHERE subject=?', ('First',)).fetchone()
            self.assertEqual(app.extract_actions(first['id'], db), 1)
            self.assertEqual(db.execute('SELECT checkpoint FROM sync_state').fetchone()[0], None)
            requests = [method for method, _ in service.calls]
            self.assertEqual(requests, ['list', 'get', 'list', 'get'])
            self.assertTrue(all(method in {'list', 'get'} for method in requests))
            get_params = [params for method, params in service.calls if method == 'get']
            self.assertTrue(all(params['format'] == 'raw' for params in get_params))
            db.close()

    def test_desktop_oauth_requests_only_readonly_and_saves_private_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client_file = root / 'client.json'
            token_file = root / 'private' / 'token.json'
            client_file.write_text('{"installed": {}}', encoding='utf-8')
            observed = {}

            class Credentials:
                valid = True
                expired = False
                refresh_token = None

                @classmethod
                def from_authorized_user_file(cls, path, scopes):
                    observed['reload'] = (path, scopes)
                    return None

                def to_json(self):
                    return '{"token":"synthetic-token"}'

            class Flow:
                @classmethod
                def from_client_secrets_file(cls, path, scopes):
                    observed['flow'] = (path, scopes)
                    return cls()

                def run_local_server(self, **kwargs):
                    observed['browser_flow'] = kwargs
                    return Credentials()

            built = object()

            def build(service, version, **kwargs):
                observed['build'] = (service, version, kwargs)
                return built

            modules = {name: ModuleType(name) for name in (
                'google', 'google.auth', 'google.auth.transport',
                'google.auth.transport.requests', 'google.oauth2',
                'google.oauth2.credentials', 'google_auth_oauthlib',
                'google_auth_oauthlib.flow', 'googleapiclient',
                'googleapiclient.discovery',
            )}
            modules['google.auth.transport.requests'].Request = object
            modules['google.oauth2.credentials'].Credentials = Credentials
            modules['google_auth_oauthlib.flow'].InstalledAppFlow = Flow
            modules['googleapiclient.discovery'].build = build
            with patch.dict('sys.modules', modules):
                self.assertIs(authorize_gmail(client_file, token_file), built)

            self.assertEqual(observed['flow'][1], [GMAIL_READONLY_SCOPE])
            self.assertEqual(observed['browser_flow'], {'port': 0, 'access_type': 'offline'})
            self.assertEqual(observed['build'][:2], ('gmail', 'v1'))
            self.assertEqual(token_file.read_text(encoding='utf-8'), '{"token":"synthetic-token"}')
            self.assertEqual(os.stat(token_file).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(token_file.parent).st_mode & 0o777, 0o700)


if __name__ == '__main__':
    unittest.main()
