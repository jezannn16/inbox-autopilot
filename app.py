"""Local, read-only email organizer. It never sends email or executes attachments."""
from __future__ import annotations
import argparse, csv, email, email.policy, hashlib, json, re, sqlite3
from typing import Protocol, Sequence
from migrations import connect as connect_db
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

DB = Path(__file__).with_name('inbox.sqlite3')

def connect(path: Path = DB) -> sqlite3.Connection:
    return connect_db(path)

def parse_eml(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body_parts=[]
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_disposition() == 'attachment':
                continue
            if part.get_content_type() == 'text/plain':
                try: body_parts.append(part.get_content())
                except (LookupError, UnicodeDecodeError): pass
    elif msg.get_content_type() == 'text/plain':
        try: body_parts.append(msg.get_content())
        except (LookupError, UnicodeDecodeError): pass
    body='\n'.join(str(x) for x in body_parts).strip()
    date=''
    if msg.get('Date'):
        try: date=parsedate_to_datetime(str(msg['Date'])).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError): pass
    mid=str(msg.get('Message-ID') or '').strip()
    return {'message_id':mid or None, 'fingerprint':hashlib.sha256((str(msg.get('From',''))+'\0'+str(msg.get('Subject',''))+'\0'+body).encode()).hexdigest(), 'sender':str(msg.get('From',''))[:500], 'subject':str(msg.get('Subject',''))[:1000], 'sent_at':date, 'body':body, 'in_reply_to':str(msg.get('In-Reply-To') or '').strip() or None, 'references':str(msg.get('References') or '').strip(), 'headers_json':json.dumps({'reply_to':str(msg.get('Reply-To') or '')})}

def store_message(raw: bytes, db: sqlite3.Connection) -> tuple[int,bool]:
    fields=parse_eml(raw)
    refs=re.findall(r'<[^>]+>',fields['references'])
    thread_key=refs[0] if refs else fields['in_reply_to'] or fields['message_id'] or fields['fingerprint']
    db.execute('INSERT OR IGNORE INTO threads(thread_key,subject,created_at) VALUES(?,?,?)',(thread_key,fields['subject'],datetime.now(timezone.utc).isoformat()))
    cur=db.execute('INSERT OR IGNORE INTO messages(message_id,fingerprint,sender,subject,sent_at,body,imported_at,thread_key,in_reply_to,headers_json) VALUES(?,?,?,?,?,?,?,?,?,?)', (fields['message_id'],fields['fingerprint'],fields['sender'],fields['subject'],fields['sent_at'],fields['body'],datetime.now(timezone.utc).isoformat(),thread_key,fields['in_reply_to'],fields['headers_json']))
    row=db.execute('SELECT id FROM messages WHERE message_id=? OR fingerprint=?', (fields['message_id'] or '',fields['fingerprint'])).fetchone()
    return int(row['id']), cur.rowcount == 1

def import_eml(path: Path, db: sqlite3.Connection) -> tuple[int,bool]:
    if path.stat().st_size > 10_000_000: raise ValueError('Email file exceeds 10 MB')
    result=store_message(path.read_bytes(),db);db.commit();return result

class ReadOnlyMailProvider(Protocol):
    """A read-only provider surface; intentionally has no send or mutate methods."""
    name: str
    def fetch_page(self, checkpoint: str | None) -> tuple[Sequence[bytes], str | None, bool]: ...

def sync_readonly(provider: ReadOnlyMailProvider, db: sqlite3.Connection, max_pages: int = 100) -> int:
    account_key='local-readonly'
    db.execute('INSERT OR IGNORE INTO accounts(provider,external_account_id,created_at) VALUES(?,?,?)',(provider.name,account_key,datetime.now(timezone.utc).isoformat()))
    state=db.execute('SELECT checkpoint FROM sync_state WHERE provider=?',(provider.name,)).fetchone();checkpoint=state['checkpoint'] if state else None
    total=0;seen=set()
    for _ in range(max_pages):
        raw_messages,next_checkpoint,has_more=provider.fetch_page(checkpoint)
        for raw in raw_messages:store_message(raw,db);total+=1
        db.execute('INSERT INTO sync_state(provider,checkpoint,updated_at) VALUES(?,?,?) ON CONFLICT(provider) DO UPDATE SET checkpoint=excluded.checkpoint,updated_at=excluded.updated_at',(provider.name,next_checkpoint,datetime.now(timezone.utc).isoformat()))
        db.commit()
        if not has_more:break
        if next_checkpoint is None or next_checkpoint in seen:raise ValueError('Provider pagination returned a repeated or missing checkpoint')
        seen.add(next_checkpoint);checkpoint=next_checkpoint
    else:raise ValueError('Provider exceeded maximum pages; checkpoint was saved for safe continuation')
    return total

def suggest_reply(message_id: int, db: sqlite3.Connection) -> str:
    row=db.execute('SELECT subject FROM messages WHERE id=?',(message_id,)).fetchone()
    if row is None:raise ValueError('Message not found')
    subject=(row['subject'] or 'your message').replace('\n',' ')[:200]
    return f"Thank you for your message about {subject}. I will review it and follow up. Please review and edit this draft before use."

# Deterministic baseline: only extract explicit action language; never infer a person/date.
ACTION_RE=re.compile(r'(?im)^\s*(?:[-*•]\s*)?(?:please\s+)?(?:todo|action|нужно|надо|пожалуйста|прошу|задача)\s*:?\s*(.{5,240})$')
OWNER_RE=re.compile(r'(?i)\b(?:owner|владелец)\s*[:=]\s*([\wА-Яа-яЁё .-]{2,60})')
DATE_RE=re.compile(r'(?i)\b(?:due|срок|дедлайн)\s*[:=]\s*(\d{4}-\d{2}-\d{2})\b')
def extract_actions(message_id: int, db: sqlite3.Connection) -> int:
    row=db.execute('SELECT body FROM messages WHERE id=?',(message_id,)).fetchone()
    if row is None: raise ValueError('Message not found')
    count=0
    for m in ACTION_RE.finditer(row['body']):
        evidence=m.group(0).strip()[:300]; action=m.group(1).strip()
        owner=OWNER_RE.search(evidence); due=DATE_RE.search(evidence)
        action=re.sub(r'(?i)[;, ]*\b(?:owner|владелец)\s*[:=].*$','',action).strip(' ;,')
        action=re.sub(r'(?i)[;, ]*\b(?:due|срок|дедлайн)\s*[:=].*$','',action).strip(' ;,')
        db.execute('INSERT OR IGNORE INTO action_items(message_id,action,owner,due_date,confidence,evidence) VALUES(?,?,?,?,?,?)',(message_id,action,owner.group(1).strip() if owner else None,due.group(1) if due else None,0.85,evidence))
        count+=1
    db.commit(); return count

def main() -> None:
    p=argparse.ArgumentParser(description='Local .eml inbox organizer (read-only; no sending)')
    p.add_argument('--db',type=Path,default=DB); sub=p.add_subparsers(dest='cmd',required=True)
    i=sub.add_parser('import'); i.add_argument('file',type=Path)
    l=sub.add_parser('list'); l.add_argument('--query',default='')
    e=sub.add_parser('extract'); e.add_argument('message_id',type=int)
    th=sub.add_parser('thread'); th.add_argument('message_id',type=int)
    d=sub.add_parser('draft'); d.add_argument('message_id',type=int); d.add_argument('text',nargs='?'); d.add_argument('--suggest',action='store_true')
    x=sub.add_parser('export'); x.add_argument('file',type=Path)
    rm=sub.add_parser('delete'); rm.add_argument('message_id',type=int); rm.add_argument('--confirm',action='store_true')
    a=p.parse_args(); db=connect(a.db)
    if a.cmd=='import':
        mid,new=import_eml(a.file,db); print(json.dumps({'id':mid,'imported':new},ensure_ascii=False))
    elif a.cmd=='list':
        rows=db.execute('SELECT id,sender,subject,sent_at FROM messages WHERE subject LIKE ? OR sender LIKE ? ORDER BY id DESC', (f'%{a.query}%',f'%{a.query}%')).fetchall()
        print(json.dumps([dict(r) for r in rows],ensure_ascii=False,indent=2))
    elif a.cmd=='extract':
        extract_actions(a.message_id,db); print(json.dumps([dict(r) for r in db.execute('SELECT * FROM action_items WHERE message_id=?',(a.message_id,))],ensure_ascii=False,indent=2))
    elif a.cmd=='thread':
        row=db.execute('SELECT thread_key FROM messages WHERE id=?',(a.message_id,)).fetchone()
        if row is None:p.error('Message not found')
        rows=db.execute('SELECT id,sender,subject,sent_at FROM messages WHERE thread_key=? ORDER BY sent_at,id',(row['thread_key'],)).fetchall()
        print(json.dumps([dict(r) for r in rows],ensure_ascii=False,indent=2))
    elif a.cmd=='draft':
        text=suggest_reply(a.message_id,db) if a.suggest else (a.text or '')
        if not text.strip(): p.error('Provide draft text or use --suggest')
        db.execute('INSERT INTO drafts(message_id,body,created_at) VALUES(?,?,?)',(a.message_id,text,datetime.now(timezone.utc).isoformat())); db.commit(); print('Draft saved locally. Sending is not implemented.')
    elif a.cmd=='delete':
        if not a.confirm: p.error('Repeat with --confirm to delete the message and related local records')
        cur=db.execute('DELETE FROM messages WHERE id=?',(a.message_id,)); db.commit()
        print('Deleted' if cur.rowcount else 'Message not found')
    elif a.cmd=='export':
        rows=db.execute('SELECT id,message_id,sender,subject,sent_at,body FROM messages ORDER BY id').fetchall()
        with a.file.open('w',encoding='utf-8',newline='') as f:
            w=csv.DictWriter(f,fieldnames=rows[0].keys() if rows else ['id','message_id','sender','subject','sent_at','body']);w.writeheader();w.writerows(map(dict,rows))
        print(f'Exported {len(rows)} message(s).')
    db.close()
if __name__=='__main__': main()
