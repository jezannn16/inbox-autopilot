import tempfile,unittest
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
