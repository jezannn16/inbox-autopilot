# Inbox Autopilot

Local MVP for importing user-provided `.eml` files, searching messages, extracting explicitly marked actions, and saving reply drafts. It never connects to a mailbox or sends email. Attachments are ignored and never executed. No credentials or network access are required.

## Install

Requires Python 3.11 or newer. Create an isolated environment and install this repository:

```sh
.venv/bin/python -m venv .venv
.venv/bin/pip install -e .
```

Use `.venv/bin/python` for the commands below.

## Quick start

```sh
.venv/bin/python app.py --db inbox.sqlite3 import sample.eml
.venv/bin/python app.py list
.venv/bin/python app.py extract 1
.venv/bin/python app.py draft 1 'Thanks, I will follow up.'
.venv/bin/python app.py export messages.csv
.venv/bin/python app.py delete 1 --confirm
```

An action is extracted only from a line beginning with `todo:`, `action:`, `нужно:`, `надо:`, `пожалуйста:` or `прошу:`. `owner:` and `due: YYYY-MM-DD` are recorded only when explicitly present in that evidence line. Confidence is a fixed baseline marker, not a calibrated probability. The SQLite database is local and contains full email bodies; protect it accordingly.

## Status and limitations

Implemented: `.eml` parsing, Message-ID/content fingerprint deduplication, thread grouping from References/In-Reply-To, search, explicit action extraction with source message/evidence, safe reply suggestion, local draft storage, CSV export/delete, a read-only paginated provider protocol with durable checkpointing. Planned: a concrete Gmail/IMAP adapter (no authorized account credentials are configured), configurable retention and calibrated extraction. No provider integration, LLM, UI, email sending, or attachment processing. Do not treat extraction as a source of truth.

## Tests

Run the standard-library test suite from this folder:

```sh
.venv/bin/python -m unittest discover -v
```
