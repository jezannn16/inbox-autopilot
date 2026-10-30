"""Local, read-only email organizer. It never sends email or executes attachments."""
from __future__ import annotations
import argparse, csv, email, email.policy, hashlib, json, re, sqlite3
from html.parser import HTMLParser
from typing import Protocol, Sequence
from migrations import connect as connect_db
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

DB = Path(__file__).with_name('inbox.sqlite3')


class _HTMLTextExtractor(HTMLParser):
    """Convert HTML message bodies to plain text without loading or executing resources."""
    _SKIP = {'script', 'style', 'head', 'noscript', 'template'}
    _BLOCK = {'address', 'article', 'blockquote', 'br', 'dd', 'div', 'dl', 'dt', 'h1', 'h2',
              'h3', 'h4', 'h5', 'h6', 'hr', 'li', 'main', 'ol', 'p', 'pre', 'section', 'table',
              'td', 'th', 'tr', 'ul'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[]
        self.skipped=[]

    def handle_starttag(self, tag, attrs):
        if self.skipped:
            if tag in self._SKIP:self.skipped.append(tag)
            return
        if tag in self._SKIP:
            self.skipped.append(tag)
        elif tag in self._BLOCK:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if self.skipped:
            if tag == self.skipped[-1]:self.skipped.pop()
        elif tag in self._BLOCK:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skipped:self.parts.append(data)

    def text(self):
        text=''.join(self.parts).replace('\xa0',' ')
        lines=[re.sub(r'[\t\f\v ]+',' ',line).strip() for line in text.splitlines()]
        compact=[]
        for line in lines:
            if line:compact.append(line)
            elif compact and compact[-1] != '':compact.append('')
        return '\n'.join(compact).strip()


def html_to_text(value: str) -> str:
    parser=_HTMLTextExtractor()
    parser.feed(value)
    parser.close()
    return parser.text()

def connect(path: Path = DB) -> sqlite3.Connection:
    return connect_db(path)

def parse_eml(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body_parts=[]
    html_parts=[]

    def collect_body(part):
        # Do not descend into an attached message/container: walk() would still
        # yield its text children and leak attachment content into the body.
        if part.get_content_disposition() == 'attachment' or part.get_filename():
            return
        if part.is_multipart():
            children=list(part.iter_parts())
            if part.get_content_subtype() == 'related' and children:
                start=part.get_param('start')
                if start:
                    target=start.strip().strip('<>')
                    root=next((child for child in children
                               if str(child.get('Content-ID') or '').strip().strip('<>') == target), children[0])
                else:
                    root=children[0]
                collect_body(root)
            else:
                for child in children:
                    collect_body(child)
            return
        if part.get_content_type() == 'text/plain':
            try: body_parts.append(part.get_content())
            except (LookupError, UnicodeDecodeError): pass
        elif part.get_content_type() == 'text/html':
            try: html_parts.append(part.get_content())
            except (LookupError, UnicodeDecodeError): pass

    collect_body(msg)
    if not body_parts:
        body_parts=[html_to_text(str(part)) for part in html_parts]
    body='\n'.join(str(x) for x in body_parts).strip()
    date=''
    if msg.get('Date'):
        try: date=parsedate_to_datetime(str(msg['Date'])).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError): pass
    mid=str(msg.get('Message-ID') or '').strip()
    return {'message_id':mid or None, 'fingerprint':hashlib.sha256((str(msg.get('From',''))+'\0'+str(msg.get('Subject',''))+'\0'+body).encode()).hexdigest(), 'sender':str(msg.get('From',''))[:500], 'subject':str(msg.get('Subject',''))[:1000], 'sent_at':date, 'body':body, 'in_reply_to':str(msg.get('In-Reply-To') or '').strip() or None, 'references':str(msg.get('References') or '').strip(), 'headers_json':json.dumps({'reply_to':str(msg.get('Reply-To') or '')})}

def store_message(raw: bytes, db: sqlite3.Connection) -> tuple[int,bool]:
    fields=parse_eml(raw)
    # Resolve identities before creating a thread: a duplicate Message-ID may
    # have changed headers and must not leave orphaned thread metadata behind.
    if fields['message_id']:
        existing=db.execute('SELECT id FROM messages WHERE message_id=?',(fields['message_id'],)).fetchone()
        if existing:return int(existing['id']),False
    existing=db.execute('SELECT id FROM messages WHERE fingerprint=?',(fields['fingerprint'],)).fetchone()
    if existing:return int(existing['id']),False
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

def sync_gmail(credentials_path: Path, token_path: Path, db: sqlite3.Connection,
               query: str = 'in:inbox', max_pages: int = 10) -> int:
    from gmail_provider import GmailProvider, authorize_gmail
    service=authorize_gmail(credentials_path,token_path)
    return sync_readonly(GmailProvider(service,query=query),db,max_pages=max_pages)

def sync_gmail_imap(db: sqlite3.Connection, username: str | None = None,
                    password: str | None = None, max_pages: int = 10,
                    page_size: int = 50, initial_window_uids: int = 1000) -> tuple[int, int]:
    import os
    from gmail_imap_provider import GmailIMAPProvider
    username = username or os.environ.get('GMAIL_IMAP_USERNAME', '')
    password = password or os.environ.get('GMAIL_IMAP_APP_PASSWORD', '')
    if not username or not password:
        raise ValueError('Set GMAIL_IMAP_USERNAME and GMAIL_IMAP_APP_PASSWORD, or use --prompt-password')
    if max_pages < 1:
        raise ValueError('Maximum page count must be positive')
    provider = GmailIMAPProvider(username, password, page_size=page_size,
                                 initial_window_uids=initial_window_uids)
    total = sync_readonly(provider, db, max_pages=max_pages)
    return total, provider.skipped_oversize

def suggest_reply(message_id: int, db: sqlite3.Connection) -> str:
    row=db.execute('SELECT sender,subject,body FROM messages WHERE id=?',(message_id,)).fetchone()
    if row is None:raise ValueError('Message not found')
    subject=(row['subject'] or 'your message').replace('\n',' ')[:200]
    excerpt=next((part.strip() for part in re.split(r'(?<=[.!?。！？])\s+|\n+',row['body']) if len(part.strip())>=8), '')[:240]
    if re.search(r'[А-Яа-яЁё]',subject+' '+excerpt):
        context=f' По вопросу: «{excerpt}».' if excerpt else ''
        return f'Спасибо за письмо о теме «{subject}». Я понял(а), что вы пишете{context} Я проверю информацию и подготовлю ответ. Проверьте и отредактируйте этот черновик перед использованием.'
    context=f' I understand your note about: “{excerpt}”.' if excerpt else ''
    return f"Thank you for your message about {subject}.{context} I will review the details and prepare a response. Please review and edit this draft before use."

def summarize_message(message_id: int, db: sqlite3.Connection) -> dict:
    """Return a bounded extractive summary; never infer promises or deadlines."""
    row=db.execute('SELECT id,subject,sender,body,thread_key FROM messages WHERE id=?',(message_id,)).fetchone()
    if row is None:raise ValueError('Message not found')
    sentences=[part.strip() for part in re.split(r'(?<=[.!?。！？])\s+|\n+',row['body']) if part.strip()]
    actions=[dict(item) for item in db.execute('SELECT id,action,owner,due_date,evidence FROM action_items WHERE message_id=? ORDER BY id',(message_id,))]
    return {'message_id':row['id'],'thread_key':row['thread_key'],'subject':row['subject'],'sender':row['sender'],
            'summary':' '.join(sentences[:2])[:500], 'explicit_actions':actions,
            'note':'Extractive summary only; no unmarked promises, owners, or dates are inferred.'}

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

def add_action_item(message_id: int, action: str, db: sqlite3.Connection,
                    owner: str | None = None, due_date: str | None = None,
                    evidence: str = '') -> tuple[int, bool]:
    """Save a reviewed action with a link to its source message and evidence."""
    if db.execute('SELECT 1 FROM messages WHERE id=?', (message_id,)).fetchone() is None:
        raise ValueError('Message not found')
    action = action.strip()
    if not action or len(action) > 500:
        raise ValueError('Action must contain 1–500 characters')
    if owner is not None and (not owner.strip() or len(owner) > 100):
        raise ValueError('Owner must contain 1–100 characters when provided')
    if due_date is not None:
        try:
            due_date = datetime.strptime(due_date, '%Y-%m-%d').date().isoformat()
        except ValueError:
            raise ValueError('Due date must be a real date in YYYY-MM-DD format') from None
    evidence = evidence.strip()[:1000]
    if not evidence:
        evidence = action
    cur = db.execute('INSERT OR IGNORE INTO action_items(message_id,action,owner,due_date,confidence,evidence) VALUES(?,?,?,?,?,?)',
                     (message_id, action, owner.strip() if owner else None, due_date, 1.0, evidence))
    row = db.execute('SELECT id FROM action_items WHERE message_id=? AND action=? AND evidence=?',
                     (message_id, action, evidence)).fetchone()
    db.commit()
    return int(row['id']), cur.rowcount == 1

def main() -> None:
    p=argparse.ArgumentParser(description='Local .eml inbox organizer (read-only; no sending)')
    p.add_argument('--db',type=Path,default=DB); sub=p.add_subparsers(dest='cmd',required=True)
    i=sub.add_parser('import'); i.add_argument('file',type=Path)
    l=sub.add_parser('list'); l.add_argument('--query',default='')
    e=sub.add_parser('extract'); e.add_argument('message_id',type=int)
    sm=sub.add_parser('summary',help='Show an extractive summary and explicitly recorded actions')
    sm.add_argument('message_id',type=int)
    ac=sub.add_parser('action-add',help='Save a reviewed action linked to a source message')
    ac.add_argument('message_id',type=int); ac.add_argument('action'); ac.add_argument('--owner'); ac.add_argument('--due'); ac.add_argument('--evidence',default='')
    al=sub.add_parser('actions'); al.add_argument('--message-id',type=int)
    dl=sub.add_parser('drafts'); dl.add_argument('--message-id',type=int)
    th=sub.add_parser('thread'); th.add_argument('message_id',type=int)
    d=sub.add_parser('draft'); d.add_argument('message_id',type=int); d.add_argument('text',nargs='?'); d.add_argument('--suggest',action='store_true')
    x=sub.add_parser('export'); x.add_argument('file',type=Path); x.add_argument('--format',choices=('csv','json'),default='csv')
    rm=sub.add_parser('delete'); rm.add_argument('message_id',type=int); rm.add_argument('--confirm',action='store_true')
    da=sub.add_parser('delete-all',help='Delete all locally stored mail data and sync checkpoints'); da.add_argument('--confirm',action='store_true')
    g=sub.add_parser('sync-gmail',help='Read messages from Gmail using local, read-only OAuth')
    g.add_argument('--credentials',type=Path,default=Path.home()/'.config/inbox-autopilot/credentials.json')
    g.add_argument('--token',type=Path,default=Path.home()/'.config/inbox-autopilot/token.json')
    g.add_argument('--query',default='in:inbox',help='Gmail search query (default: in:inbox)')
    g.add_argument('--max-pages',type=int,default=10,help='Maximum pages per run (100 messages per page)')
    gi=sub.add_parser('sync-gmail-imap',help='Optional Gmail read-only IMAP sync via a local app password')
    gi.add_argument('--username',help='Full Gmail address (or GMAIL_IMAP_USERNAME)')
    gi.add_argument('--prompt-password',action='store_true',help='Read the app password with terminal input; it is not saved')
    gi.add_argument('--max-pages',type=int,default=10)
    gi.add_argument('--page-size',type=int,default=50)
    gi.add_argument('--initial-window-uids',type=int,default=1000)
    a=p.parse_args(); db=connect(a.db)
    if a.cmd=='import':
        mid,new=import_eml(a.file,db); print(json.dumps({'id':mid,'imported':new},ensure_ascii=False))
    elif a.cmd=='list':
        rows=db.execute('SELECT id,sender,subject,sent_at FROM messages WHERE subject LIKE ? OR sender LIKE ? ORDER BY id DESC', (f'%{a.query}%',f'%{a.query}%')).fetchall()
        print(json.dumps([dict(r) for r in rows],ensure_ascii=False,indent=2))
    elif a.cmd=='extract':
        extract_actions(a.message_id,db); print(json.dumps([dict(r) for r in db.execute('SELECT * FROM action_items WHERE message_id=?',(a.message_id,))],ensure_ascii=False,indent=2))
    elif a.cmd=='summary':
        print(json.dumps(summarize_message(a.message_id,db),ensure_ascii=False,indent=2))
    elif a.cmd=='action-add':
        action_id,created=add_action_item(a.message_id,a.action,db,a.owner,a.due,a.evidence)
        print(json.dumps({'id':action_id,'created':created,'source_message_id':a.message_id},ensure_ascii=False))
    elif a.cmd=='actions':
        rows=db.execute('SELECT a.*,m.subject,m.sender FROM action_items a JOIN messages m ON m.id=a.message_id '+('WHERE a.message_id=? ' if a.message_id else '')+'ORDER BY a.id DESC', (a.message_id,) if a.message_id else ()).fetchall()
        print(json.dumps([dict(r) for r in rows],ensure_ascii=False,indent=2))
    elif a.cmd=='drafts':
        rows=db.execute('SELECT d.id,d.message_id,d.body,d.created_at,m.thread_key,m.subject FROM drafts d JOIN messages m ON m.id=d.message_id '+('WHERE d.message_id=? ' if a.message_id else '')+'ORDER BY d.id DESC', (a.message_id,) if a.message_id else ()).fetchall()
        print(json.dumps([dict(r) for r in rows],ensure_ascii=False,indent=2))
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
        cur=db.execute('DELETE FROM messages WHERE id=?',(a.message_id,))
        if cur.rowcount:
            db.execute('DELETE FROM threads WHERE thread_key NOT IN (SELECT DISTINCT thread_key FROM messages WHERE thread_key IS NOT NULL)')
        db.commit()
        print('Deleted' if cur.rowcount else 'Message not found')
    elif a.cmd=='delete-all':
        if not a.confirm: p.error('Repeat with --confirm to delete all local messages, drafts, actions, accounts and sync state')
        counts={table:db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in ('messages','action_items','drafts','threads','accounts','sync_state')}
        db.execute('DELETE FROM action_items'); db.execute('DELETE FROM drafts'); db.execute('DELETE FROM messages')
        db.execute('DELETE FROM threads'); db.execute('DELETE FROM accounts'); db.execute('DELETE FROM sync_state'); db.commit()
        print(json.dumps({'deleted':counts},ensure_ascii=False))
    elif a.cmd=='export':
        if a.format=='json':
            payload={name:[dict(r) for r in db.execute(f'SELECT * FROM {name} ORDER BY rowid')]
                     for name in ('accounts','threads','messages','action_items','drafts','sync_state')}
            a.file.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
            print(f"Exported {sum(len(rows) for rows in payload.values())} local record(s) as JSON.")
        else:
            rows=db.execute('SELECT id,message_id,sender,subject,sent_at,body FROM messages ORDER BY id').fetchall()
            with a.file.open('w',encoding='utf-8',newline='') as f:
                w=csv.DictWriter(f,fieldnames=rows[0].keys() if rows else ['id','message_id','sender','subject','sent_at','body']);w.writeheader();w.writerows(map(dict,rows))
            print(f'Exported {len(rows)} message(s) as CSV; use --format json for related local records.')
    elif a.cmd=='sync-gmail':
        if a.max_pages < 1: p.error('--max-pages must be positive')
        try: total=sync_gmail(a.credentials,a.token,db,a.query,a.max_pages)
        except (FileNotFoundError,ValueError,RuntimeError) as exc: p.error(str(exc))
        print(json.dumps({'provider':'gmail','messages_seen':total,'query':a.query},ensure_ascii=False))
    elif a.cmd=='sync-gmail-imap':
        import getpass,os
        if a.max_pages < 1:p.error('--max-pages must be positive')
        username=a.username or os.environ.get('GMAIL_IMAP_USERNAME','')
        password=os.environ.get('GMAIL_IMAP_APP_PASSWORD','')
        if not password and a.prompt_password:
            password=getpass.getpass('Gmail app password (input hidden; not saved): ')
        try:total,skipped=sync_gmail_imap(db,username,password,a.max_pages,a.page_size,a.initial_window_uids)
        except (ValueError,RuntimeError) as exc:p.error(str(exc))
        print(json.dumps({'provider':'gmail-imap','messages_seen':total,'oversize_skipped':skipped,'mailbox':'INBOX'},ensure_ascii=False))
    db.close()
if __name__=='__main__': main()
