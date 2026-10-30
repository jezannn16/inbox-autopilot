# Demo

Use a temporary database and the synthetic message fixture:

```sh
python3 app.py --db /tmp/inbox-demo.sqlite3 import fixtures/sample.eml
python3 app.py --db /tmp/inbox-demo.sqlite3 list --query 'project notes' --sender example.test --since 2026-10-29 --has-actions
python3 app.py --db /tmp/inbox-demo.sqlite3 extract 1
python3 app.py --db /tmp/inbox-demo.sqlite3 draft 1 'Thanks, I will share the notes.'
python3 app.py --db /tmp/inbox-demo.sqlite3 export /tmp/inbox-demo.csv
```

Repeat the import to see deduplication (`imported: false`). The draft is stored locally; there is no send command.
