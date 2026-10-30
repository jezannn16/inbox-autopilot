import tempfile,unittest,json,subprocess,sys
from pathlib import Path
import app
class InboxTests(unittest.TestCase):
 def test_import_deduplicates_and_extracts_only_explicit_action(self):
  with tempfile.TemporaryDirectory() as td:
   f=Path(td)/'a.eml';f.write_text('From: a@example.test\nSubject: Plan\nMessage-ID: <x>\n\nPlease do not assume anything.\nTODO: Send the agenda; Owner: Alex; Due: 2026-11-01',encoding='utf-8')
   db=app.connect(Path(td)/'db.sqlite');mid,new=app.import_eml(f,db);again,new2=app.import_eml(f,db)
   self.assertTrue(new);self.assertFalse(new2);self.assertEqual(mid,again)
   self.assertEqual(app.extract_actions(mid,db),1);r=db.execute('select * from action_items').fetchone()
   self.assertEqual(r['action'],'Send the agenda');self.assertEqual(r['owner'],'Alex');self.assertEqual(r['due_date'],'2026-11-01');db.close()
 def test_empty_message_body_is_valid(self):
  self.assertEqual(app.parse_eml(b'Subject: Empty\n\n')['body'],'')
 def test_manual_action_is_validated_deduplicated_and_keeps_source(self):
  with tempfile.TemporaryDirectory() as td:
   f=Path(td)/'mail.eml';f.write_text('From: a@example.test\nSubject: Review\n\nPlease review this.',encoding='utf-8')
   db=app.connect(Path(td)/'mail.db');mid,_=app.import_eml(f,db)
   aid,created=app.add_action_item(mid,'Review the proposal',db,owner='Alex',due_date='2026-11-01',evidence='Please review this.')
   self.assertTrue(created);again,created_again=app.add_action_item(mid,'Review the proposal',db,owner='Alex',due_date='2026-11-01',evidence='Please review this.')
   self.assertEqual((aid,created_again),(again,False));row=db.execute('SELECT message_id,owner,due_date,evidence FROM action_items WHERE id=?',(aid,)).fetchone()
   self.assertEqual((row['message_id'],row['owner'],row['due_date'],row['evidence']),(mid,'Alex','2026-11-01','Please review this.'))
   with self.assertRaises(ValueError):app.add_action_item(mid,'Bad date',db,due_date='2026-02-30')
   db.close()
 def test_full_json_export_and_confirmed_delete_all(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);source=root/'mail.eml';source.write_text('From: a@example.test\nSubject: privacy\nMessage-ID: <privacy>\n\nTODO: Keep this synthetic',encoding='utf-8')
   db=app.connect(root/'mail.db');mid,_=app.import_eml(source,db);app.extract_actions(mid,db);db.execute('INSERT INTO drafts(message_id,body,created_at) VALUES(?,?,?)',(mid,'Synthetic draft','now'));db.commit();db.close()
   export=root/'all.json';run=subprocess.run([sys.executable,'app.py','--db',str(root/'mail.db'),'export',str(export),'--format','json'],capture_output=True,text=True)
   self.assertEqual(run.returncode,0,run.stderr);payload=json.loads(export.read_text(encoding='utf-8'))
   self.assertEqual(len(payload['messages']),1);self.assertEqual(len(payload['action_items']),1);self.assertEqual(len(payload['drafts']),1)
   no_confirm=subprocess.run([sys.executable,'app.py','--db',str(root/'mail.db'),'delete-all'],capture_output=True,text=True)
   self.assertNotEqual(no_confirm.returncode,0)
   deleted=subprocess.run([sys.executable,'app.py','--db',str(root/'mail.db'),'delete-all','--confirm'],capture_output=True,text=True)
   self.assertEqual(deleted.returncode,0,deleted.stderr);check=app.connect(root/'mail.db')
   self.assertEqual(check.execute('SELECT COUNT(*) FROM messages').fetchone()[0],0);self.assertEqual(check.execute('SELECT COUNT(*) FROM drafts').fetchone()[0],0);check.close()
 def test_provider_pagination_checkpoint_and_restart_deduplication(self):
  raw1=b'From: a@example.test\nSubject: First\nMessage-ID: <first@example.test>\n\nOne'
  raw2=b'From: a@example.test\nSubject: Reply\nMessage-ID: <reply@example.test>\nIn-Reply-To: <first@example.test>\nReferences: <first@example.test>\n\nTwo'
  class FakeProvider:
   name='fake-readonly'
   def __init__(self):self.calls=[]
   def fetch_page(self,checkpoint):
    self.calls.append(checkpoint)
    if checkpoint is None:return [raw1],'cursor-1',True
    if checkpoint=='cursor-1':return [raw2],'cursor-2',False
    return [],checkpoint,False
  with tempfile.TemporaryDirectory() as td:
   db=app.connect(Path(td)/'sync.db');provider=FakeProvider()
   self.assertEqual(app.sync_readonly(provider,db),2);self.assertEqual(provider.calls,[None,'cursor-1'])
   self.assertEqual(db.execute('select count(*) from messages').fetchone()[0],2)
   self.assertEqual(db.execute('select count(distinct thread_key) from messages').fetchone()[0],1)
   self.assertEqual(db.execute('select checkpoint from sync_state').fetchone()[0],'cursor-2')
   db.close()
if __name__=='__main__':unittest.main()
