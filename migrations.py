"""Versioned SQLite migrations for imported email, threads, and review artifacts."""
from __future__ import annotations
import sqlite3
from datetime import datetime,timezone

def _v1(db):
 db.executescript('''CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,message_id TEXT UNIQUE,fingerprint TEXT UNIQUE,sender TEXT,subject TEXT,sent_at TEXT,body TEXT NOT NULL,imported_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS action_items(id INTEGER PRIMARY KEY,message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,action TEXT NOT NULL,owner TEXT,due_date TEXT,confidence REAL NOT NULL,evidence TEXT NOT NULL,UNIQUE(message_id,action,evidence));
 CREATE TABLE IF NOT EXISTS drafts(id INTEGER PRIMARY KEY,message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,body TEXT NOT NULL,created_at TEXT NOT NULL);''')
def _v2(db):
 db.executescript('''CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY,provider TEXT NOT NULL,external_account_id TEXT,created_at TEXT NOT NULL,UNIQUE(provider,external_account_id));
 CREATE TABLE IF NOT EXISTS threads(id INTEGER PRIMARY KEY,thread_key TEXT NOT NULL UNIQUE,subject TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS sync_state(provider TEXT PRIMARY KEY,checkpoint TEXT,updated_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS telegram_updates(update_id INTEGER PRIMARY KEY,processed_at TEXT NOT NULL);''')
 cols={r[1] for r in db.execute('PRAGMA table_info(messages)')}
 if 'thread_key' not in cols:db.execute('ALTER TABLE messages ADD COLUMN thread_key TEXT')
 if 'in_reply_to' not in cols:db.execute('ALTER TABLE messages ADD COLUMN in_reply_to TEXT')
 if 'headers_json' not in cols:db.execute('ALTER TABLE messages ADD COLUMN headers_json TEXT NOT NULL DEFAULT \'{}\'')
def connect(path):
 db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA busy_timeout=5000')
 db.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)')
 current=db.execute('SELECT COALESCE(MAX(version),0) FROM schema_migrations').fetchone()[0]
 for version,migration in enumerate((_v1,_v2),1):
  if version>current:
   migration(db);db.execute('INSERT INTO schema_migrations VALUES(?,?)',(version,datetime.now(timezone.utc).isoformat()));db.commit()
 return db
