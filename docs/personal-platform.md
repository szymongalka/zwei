# Personal add-ons

The optional `personal` extension adds one **Add-ons / Dodatki** view with tabs for
the unified inbox, Apple accounts, the agent mailbox, other mailboxes, memory and
development. It is disabled by default. This fork patch is based on upstream
`499bf903022f429dd4501fdfbeeccadcb99dd51f`; future upstream integrations still need
the compatibility checks in [fork maintenance](./fork-maintenance.md).

## Installation and configuration

Install from this fork in editable mode: `uv pip install --python .venv/bin/python -e '.[personal]'`.
Build the normal WebUI with `cd webui && bun install --frozen-lockfile && bun run build`.
The extension uses existing gateway authentication, HTTP status reads and
authenticated WebSocket mutations; it needs no additional public port or proxy.

Add this section to the existing nanobot configuration, using your real paths:

```json
{
  "personal": {
    "enabled": true,
    "dataDir": "~/.nanobot/personal",
    "postgresFile": "/absolute/path/to/postgres.json",
    "syncIntervalSeconds": 300,
    "syncBatchSize": 50,
    "evolutionEnabled": true,
    "evolutionIntervalSeconds": 21600,
    "developmentEnabled": true,
    "developmentIntervalSeconds": 86400,
    "developmentTimeoutSeconds": 1200
  }
}
```

Restart the gateway after enabling/disabling the extension or changing its worker
configuration. Without `postgresFile`, the local archive and lexical search work;
remote vector retrieval is unavailable. Install PostgreSQL with pgvector on the
memory server. The server-side connection JSON uses psycopg keys: `host`, `port`,
`dbname`, `user`, `password`, `sslmode`, `sslrootcert`, `connect_timeout`.
`sslmode` must be `verify-full`; protect that file with mode 600. Do not put
credentials in the workspace, repository, chat or browser storage.

The embedding model is `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
(384 dimensions), downloaded on first use and evaluated locally through FastEmbed.
The installed optional dependency pins FastEmbed to the 0.8 series. Model files
are cached under `dataDir/models`; the gateway shares the loaded model between
the worker and WebUI. No embedding API key or external embedding request is used.

## Accounts and the unified inbox

Create accounts in their Add-ons tabs. Apple setup accepts an app-specific password
and provides CalDAV, CardDAV and mail endpoint defaults. Collection through DAV is
read-only. Mail uses verified IMAP TLS; SMTP requires STARTTLS or TLS. Connection
checks authenticate and discover resources but do not send messages.

Agent mailboxes allow sending by default. Other mailboxes and Apple accounts need
their own sending toggle. An account can be disabled without deleting its archive.
Blank password fields during editing preserve saved credentials; list responses
contain presence flags, never passwords. Saved accounts are encrypted with a local
Fernet key. This protects stored records and accidental disclosure, not a host
administrator who also has access to the key.

Use **Include in unified inbox** on each selected account. The view merges matching
copies, displays their source accounts, supports pagination and sorting by date or
sender, and renders mail as text without active HTML or tracking images. It shows
the collected archive; it does not mirror read flags, deletions or every live IMAP
change. Initial collection proceeds incrementally, normally 50 messages per folder
every five minutes. Oversized messages stop that folder's cursor rather than being
silently skipped; raise the account's size limit if needed.

**No organization system is imposed.** `organize_folders` defaults to false and
`folder_rules` to an empty list. The UI deliberately does not enable moves or
invent categories. When the owner defines a policy, account-specific rules can
match literal text/senders and map to exact folders through the typed account API.
The engine reuses those folders, creating a missing destination only after an
explicit rule requires it. It adds no parent group. Moves require an archived raw
message, IMAP MOVE or UIDPLUS, and recorded operation state. Fallback uses targeted
UID EXPUNGE, never global EXPUNGE. Uncertain moves are recorded and not blindly
repeated. Rules added later do not automatically move previously collected mail.

Sending uses a stable operation ID. A confirmed retry returns the previous result;
an uncertain acknowledgement requires inspection before another send. SMTP cannot
provide universal exactly-once delivery. The extension does not append a Sent
folder copy itself; servers may do that, and the local archive retains the outgoing
content and delivery result. The agent tool's sending capability is not permission
to send arbitrary mail; use the owner's authorization for each workflow.

## Memory and compaction

`dataDir/archive.sqlite3` is a SQLite WAL archive with full synchronous commits.
Raw JSON and RFC822 records are compressed, content-addressed, namespace-scoped
and protected from update/delete. Ordered session snapshots have durable receipts.
FTS and vector chunks are search projections; they do not replace raw records.
The encryption key and database use mode 600 in a directory created with mode 700.

The archive receives completed conversation turns and the exact messages available
before compaction, including the retained session transcript. If local archival
fails, the compaction hook raises before the summary proceeds. PostgreSQL copies
raw documents, snapshot manifests and vector chunks asynchronously. Remote failure
leaves an outbox; acknowledgements follow commit, and lexical retrieval remains
available. Non-persistent/private sessions are excluded.

Native `SOUL.md`, `USER.md`, `memory/MEMORY.md`, `memory/EVOLUTION.md` and history
records are also indexed without modifying them. Existing Dream and memory files
retain their original roles. Corrections and later versions remain distinguishable
by source and time. This cannot reconstruct content already lost before installation
or bytes represented only by an external attachment path. Keep the original media
storage backed up too. Retrieval can still miss a fact; full archival is not a
guarantee that every answer recalls every detail.

The `personal_archive` agent tool exposes status, search, paged reads, the inbox,
saved-account synchronization and authorized sending. Retrieved context is bounded
and explicitly marked as untrusted source material. Account administration remains
in the authenticated WebUI.

## Autonomous development

Three mechanisms cooperate: existing Dream consolidates profiles, memory and skills;
bounded retrieval experiments compare lexical/hybrid/semantic ranking against a
held-out set; a daily agent turn investigates one evidenced recurring problem and
can improve reusable skills or optional fork code within the owner's recorded scope.
The development worker is separate from ingestion and has a time limit. Its base
instructions are `nanobot/personal/development.md`; workspace-specific instructions
belong in `prompts/development.md`. It uses the existing agent, tools and session
journal, and reports results in the Development tab. It does not train hosted model
weights. A completed turn is not evidence that an improvement was deployed: inspect
its recorded tests, revision and measured result.

Retrieval experiments require at least 12 examples by default. They promote a
candidate only when both training and held-out source-retrieval MRR improve, keep
the previous policy, and support rollback. These are self-supervised search probes,
not human satisfaction measurements. All experiments, including rejected ones,
remain in the journal. Development must preserve upstream compatibility, validate
changes in isolated worktrees, and honor existing CI/review and access rules.

## Operations, testing and rollback

Back up SQLite using its backup API rather than copying an active database alone.
Keep the matching `accounts.key`, connection JSON, configuration, workspace and
media. Use a consistent PostgreSQL dump with pgvector installed for restoration.
Protect backups like credentials and test restoration to a separate database/path.
Reindexing is possible from raw records; do not discard raw data when changing a
projection or embedding model.

To disable: set `personal.enabled` false and restart. This hides navigation, stops
workers and removes the archive hook; saved data remains. Roll back source and
frontend assets together to the previously verified revision. Restore the previous
configuration when rolling back before this extension existed.

The patch has focused tests under `tests/personal/` and
`webui/src/tests/personal-addons.test.tsx`, plus existing gateway, memory and WebUI
regressions. Live account protocol validation still requires the owner's actual
credentials; simulated protocol tests cannot prove a particular provider's login.

Integration points: `config/schema.py`, `cli/gateway_runtime.py`, the optional
`MemoryStore.archive_sink`, `webui/ws_http.py`, `App.tsx` and `Sidebar.tsx`.
Feature logic lives in `nanobot/personal/`, `webui/personal_routes.py` and
`webui/src/addons/personal/`. There is no generic dynamic plugin loader.
