"""Durable local archive and outbox; raw records are independent of search projections."""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import os
import re
import sqlite3
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from cryptography.fernet import Fernet
from filelock import FileLock

from nanobot.personal.config import Account
from nanobot.personal.visibility import HOT, classify
from nanobot.runtime_context import (
    RUNTIME_CONTEXT_TAG,
    public_history_message,
    strip_runtime_context_envelope,
)

#: Append-only archive of every record that left the curated layer; compaction
#: writes it and retrieval reads it only for an explicit forensic request.
EVIDENCE_ARCHIVE = "archive.evidence.sqlite3"

#: The archive schema, shared by the live store and by offline compaction, which
#: builds an equivalent file for the evidence layer.
ARCHIVE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS documents (
        id TEXT PRIMARY KEY, namespace TEXT NOT NULL, source TEXT NOT NULL,
        item_key TEXT NOT NULL, payload BLOB NOT NULL, text TEXT NOT NULL,
        created TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'hot'
    );
    CREATE INDEX IF NOT EXISTS documents_scope ON documents(namespace, source, item_key);
    CREATE VIRTUAL TABLE IF NOT EXISTS document_search USING fts5(id UNINDEXED, text);
    CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, synced TEXT);
    CREATE TABLE IF NOT EXISTS document_versions (
        id TEXT PRIMARY KEY, source TEXT NOT NULL, item_key TEXT NOT NULL,
        version TEXT NOT NULL, superseded TEXT, created TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS document_versions_scope
        ON document_versions(source, item_key);
    CREATE TABLE IF NOT EXISTS snapshots (
        id TEXT PRIMARY KEY, namespace TEXT NOT NULL, session_key TEXT NOT NULL,
        reason TEXT NOT NULL, document_ids BLOB NOT NULL, created TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS snapshot_outbox (id TEXT PRIMARY KEY, synced TEXT);
    CREATE TABLE IF NOT EXISTS accounts (
        id TEXT PRIMARY KEY, namespace TEXT NOT NULL, encrypted BLOB NOT NULL,
        updated TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS checkpoints (
        namespace TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
        PRIMARY KEY(namespace,key)
    );
    CREATE TABLE IF NOT EXISTS operations (
        id TEXT PRIMARY KEY, namespace TEXT NOT NULL, kind TEXT NOT NULL,
        status TEXT NOT NULL, detail TEXT NOT NULL, created TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS evolution (
        id INTEGER PRIMARY KEY, namespace TEXT NOT NULL, created TEXT NOT NULL,
        status TEXT NOT NULL, payload TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS inbox (
        id TEXT PRIMARY KEY REFERENCES documents(id), namespace TEXT NOT NULL,
        account_id TEXT NOT NULL, logical_key TEXT NOT NULL, sender TEXT NOT NULL,
        subject TEXT NOT NULL, sent_at TEXT NOT NULL, category TEXT NOT NULL,
        priority INTEGER NOT NULL, preview TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS inbox_order ON inbox(namespace,category,priority,sent_at);
    CREATE TRIGGER IF NOT EXISTS documents_no_update BEFORE UPDATE ON documents
    BEGIN SELECT RAISE(ABORT, 'Raw archive records are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS documents_no_delete BEFORE DELETE ON documents
    BEGIN SELECT RAISE(ABORT, 'Raw archive records are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS snapshots_no_delete BEFORE DELETE ON snapshots
    BEGIN SELECT RAISE(ABORT, 'Archive snapshots are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS snapshots_no_update BEFORE UPDATE ON snapshots
    BEGIN SELECT RAISE(ABORT, 'Archive snapshots are immutable'); END;
"""

#: Tables that describe the archive itself rather than one record; compaction
#: keeps them in the live file.
ARCHIVE_METADATA_TABLES = ("document_versions", "snapshots", "accounts", "checkpoints",
                           "operations", "evolution", "inbox")


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def searchable_text(value: object) -> str:
    """Build a bounded-output *projection* without changing the raw archive."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(searchable_text(item) for item in cast(list[object], value))
    if isinstance(value, dict):
        return "\n".join(
            f"{key}: {searchable_text(item)}" for key, item in cast(dict[str, object], value).items()
            if key not in {"data", "base64", "password", "token", "api_key", "apiKey"}
            and not str(key).endswith("_b64")
        )
    return str(value) if value is not None else ""


def readable_excerpt(text: str, limit: int) -> str:
    """Whitespace-condensed retrieval projection; the raw archive stays untouched.

    Archived mail bodies keep their stored form, so markup remnants such as
    entities and line-break runs would otherwise dominate excerpts. Returned
    text is never longer than ``limit`` characters and may be empty.
    """
    condensed = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return condensed[: max(0, limit)]


def message_projection(message: Mapping[str, Any]) -> str:
    """Searchable projection of one persisted message, without injected context.

    The raw record keeps the runtime-context suffix; only the indexed projection
    drops it, so retrieval cannot quote the archive's own injections back.
    """
    return strip_runtime_context_envelope(searchable_text(public_history_message(message)))


def projection_text(payload: object) -> str:
    """Searchable projection of any payload with runtime-context envelopes removed."""
    return strip_runtime_context_envelope(searchable_text(payload))


class PersonalStore:
    def __init__(self, directory: Path, workspace: Path):
        self.directory = directory.expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.namespace = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:24]
        self.path = self.directory / "archive.sqlite3"
        # The evidence layer lives beside the live archive once compaction has run.
        self.evidence_path = self.directory / EVIDENCE_ARCHIVE
        self.key_path = self.directory / "accounts.key"
        with FileLock(str(self.key_path) + ".lock"):
            if not self.key_path.exists():
                fd = os.open(self.key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as out:
                    out.write(Fernet.generate_key())
                    out.flush()
                    os.fsync(out.fileno())
        self._cipher = Fernet(self.key_path.read_bytes())
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(ARCHIVE_SCHEMA)
            self._migrate_visibility(db)
            db.execute(
                "CREATE INDEX IF NOT EXISTS documents_visibility "
                "ON documents(namespace, visibility, source)")
        self.path.chmod(0o600)

    def _migrate_visibility(self, db: sqlite3.Connection) -> None:
        """Backfill the corpus layer of archives written before the split.

        The raw-record trigger is dropped for the duration of the backfill and
        recreated immediately; a crash in between leaves the archive readable and
        the next start repeats the migration, because the column is still missing.
        """
        columns = {row[1] for row in db.execute("PRAGMA table_info(documents)")}
        if "visibility" in columns:
            return
        db.executescript("DROP TRIGGER IF EXISTS documents_no_update;")
        db.execute("ALTER TABLE documents ADD COLUMN visibility TEXT NOT NULL DEFAULT 'hot'")
        rows = db.execute("SELECT id, source, payload FROM documents").fetchall()
        layers = [(row["id"], self._layer_of(row["source"], row["payload"])) for row in rows]
        db.execute("CREATE TEMP TABLE IF NOT EXISTS archive_layers(id TEXT PRIMARY KEY, layer TEXT)")
        db.executemany("INSERT OR REPLACE INTO archive_layers VALUES (?,?)", layers)
        db.execute(
            "UPDATE documents SET visibility=(SELECT layer FROM archive_layers "
            "WHERE archive_layers.id=documents.id) "
            "WHERE id IN (SELECT id FROM archive_layers)")
        db.executescript("""
            DROP TABLE archive_layers;
            CREATE TRIGGER documents_no_update BEFORE UPDATE ON documents
            BEGIN SELECT RAISE(ABORT, 'Raw archive records are immutable'); END;
        """)

    @staticmethod
    def _layer_of(source: str, payload: bytes) -> str:
        if not source.startswith("session:"):
            # Only a transcript needs its payload read: the layer of every other
            # source follows from the source alone, which keeps the migration cheap.
            return classify(source, None)
        try:
            decoded: object = json.loads(gzip.decompress(payload))
        except (OSError, ValueError):
            decoded = None
        return classify(source, decoded)

    @contextmanager
    def db(self) -> Generator[sqlite3.Connection]:
        with self._connect(self.path) as db:
            yield db

    @contextmanager
    def _connect(self, path: Path) -> Generator[sqlite3.Connection]:
        db = sqlite3.connect(path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _put(self, db: sqlite3.Connection, source: str, key: str, payload: object,
             text: str | None = None, version: str | None = None,
             visibility: str | None = None) -> str:
        raw = canonical(payload)
        identity = canonical([self.namespace, source, key]) + raw
        identifier = hashlib.sha256(identity).hexdigest()
        projection = searchable_text(payload) if text is None else text
        layer = visibility or classify(source, payload)
        inserted = db.execute(
            "INSERT OR IGNORE INTO documents VALUES (?,?,?,?,?,?,?,?)",
            (identifier, self.namespace, source, key, gzip.compress(raw, mtime=0), projection,
             utcnow(), layer),
        ).rowcount
        if inserted:
            db.execute("INSERT INTO document_search VALUES (?,?)", (identifier, projection))
            if layer == HOT:
                # Only the curated layer is projected to the remote vector index.
                db.execute("INSERT INTO outbox(id) VALUES (?)", (identifier,))
        if version is not None:
            self._record_version(db, source, key, identifier, version)
        return identifier

    def _record_version(self, db: sqlite3.Connection, source: str, key: str,
                        identifier: str, version: str) -> None:
        """Keep one current version per item and re-queue the stale copies for cleanup.

        Raw records stay immutable, so a changed item produces a second document.
        The projection, not the archive, follows the newest version: older rows are
        marked here and dropped from search results and from the remote index.
        """
        db.execute("INSERT OR IGNORE INTO document_versions VALUES (?,?,?,?,?,?)",
                   (identifier, source, key, version, None, utcnow()))
        stale = [row[0] for row in db.execute(
            "SELECT id FROM document_versions WHERE source=? AND item_key=? AND id<>? AND superseded IS NULL",
            (source, key, identifier)).fetchall()]
        if not stale:
            return
        db.execute("UPDATE document_versions SET superseded=? WHERE source=? AND item_key=? AND id<>?",
                   (identifier, source, key, identifier))
        db.executemany("UPDATE outbox SET synced=NULL WHERE id=?", [(item,) for item in stale])

    def put(self, source: str, key: str, payload: object, text: str | None = None,
            version: str | None = None) -> str:
        with self.db() as db:
            return self._put(db, source, key, payload, text, version)

    def prepare_delivery(self, account_id: str, operation_id: str, digest: str, payload: object) -> bool:
        """Reserve a send and archive its content atomically; True means already sent."""
        with self.db() as db:
            old = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if old:
                if old["namespace"] != self.namespace or old["detail"] != digest:
                    raise ValueError("Operation ID belongs to another message")
                if old["status"] == "sent":
                    return True
                raise ValueError("Prior delivery is uncertain; automatic retry is blocked")
            self._put(db, "outgoing:" + account_id, operation_id, payload)
            db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?)",
                       (operation_id, self.namespace, "smtp", "sending", digest, utcnow()))
        return False

    def archive_messages(self, session_key: str, messages: list[dict[str, Any]],
                         reason: str = "pre-compaction") -> str:
        """A receipt exists only after the complete snapshot transaction is durable."""
        with self.db() as db:
            identifiers = [self._put(db, "session:" + session_key, str(index), message,
                                     text=message_projection(message))
                           for index, message in enumerate(messages)]
            raw = canonical(identifiers)
            identity = hashlib.sha256(canonical([self.namespace, session_key, reason]) + raw).hexdigest()
            db.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?,?)", (
                identity, self.namespace, session_key, reason, gzip.compress(raw, mtime=0), utcnow(),
            ))
            db.execute("INSERT OR IGNORE INTO snapshot_outbox(id) VALUES (?)", (identity,))
        return identity

    def get(self, identifier: str) -> dict[str, Any]:
        """Read one raw record from the live archive or, failing that, from evidence."""
        row = self._document(self.path, identifier)
        if row is None:
            row = self._document(self.evidence_path, identifier)
        if row is None:
            raise ValueError("Memory record not found")
        return {"id": row["id"], "source": row["source"], "key": row["item_key"],
                "created": row["created"], "visibility": row["visibility"],
                "payload": json.loads(gzip.decompress(row["payload"]))}

    def _document(self, path: Path, identifier: str) -> sqlite3.Row | None:
        if not path.is_file():
            return None
        with self._connect(path) as db:
            return db.execute("SELECT * FROM documents WHERE id=? AND namespace=?",
                              (identifier, self.namespace)).fetchone()

    def search(self, query: str, limit: int = 10, *,
               exclude_prefixes: Sequence[str] = (),
               visibilities: Sequence[str] = (HOT,),
               include_evidence_file: bool = False) -> list[dict[str, Any]]:
        """Rank lexical hits inside the requested corpus layers.

        ``visibilities`` selects the layers (``hot`` by default, ``hot``+``cold``
        for an explicit forensic request); ``exclude_prefixes`` filters by source
        prefix, both before the limit is applied, so a scope never spends its
        budget on a layer it does not want.  ``include_evidence_file`` also
        searches the immutable evidence archive written by compaction, which is
        where transcripts live once the live file has been rebuilt.  Ranking is
        unchanged within a file, and live hits precede evidence hits.
        """
        results = self._search_file(self.path, query, limit, exclude_prefixes, visibilities)
        if include_evidence_file and len(results) < limit and self.evidence_path.is_file():
            results += self._search_file(self.evidence_path, query, limit - len(results),
                                         exclude_prefixes, visibilities)
        return results

    def _search_file(self, path: Path, query: str, limit: int,
                     exclude_prefixes: Sequence[str],
                     visibilities: Sequence[str]) -> list[dict[str, Any]]:
        words = re.findall(r"\w+", query, re.UNICODE)[:20]
        if not words or limit <= 0 or not visibilities:
            return []
        expression = " OR ".join('"' + word + '"' for word in words)
        layers = ",".join("?" for _ in visibilities)
        exclusion = "".join(" AND d.source NOT LIKE ?" for _ in exclude_prefixes)
        parameters: list[object] = [expression, self.namespace, *visibilities]
        parameters.extend(f"{prefix}%" for prefix in exclude_prefixes)
        parameters.append(max(1, min(limit, 100)))
        with self._connect(path) as db:
            rows = db.execute(f"""
                SELECT d.id,d.source,d.item_key,document_search.text,d.created,d.visibility
                FROM document_search JOIN documents d ON d.id=document_search.id
                WHERE document_search MATCH ? AND d.namespace=?
                  AND d.visibility IN ({layers})
                  AND d.id NOT IN (SELECT id FROM document_versions WHERE superseded IS NOT NULL)
                  {exclusion}
                ORDER BY bm25(document_search) LIMIT ?
            """, parameters).fetchall()
        return [{"id": r["id"], "source": r["source"], "key": r["item_key"],
                 "excerpt": r["text"][:1800], "created": r["created"],
                 "visibility": r["visibility"]} for r in rows]

    def rebuild_search_projections(self, limit: int = 2000) -> dict[str, int]:
        """Recompute search projections that still index injected runtime context.

        Raw records stay immutable (and the archive triggers enforce that); only
        the rebuildable projection and the remote re-queue change.  Idempotent:
        a second run over the same rows finds nothing to repair.
        """
        with self.db() as db:
            rows = db.execute(
                """SELECT d.id, d.payload, d.text AS raw_text, s.text AS projection
                   FROM documents d LEFT JOIN document_search s ON s.id = d.id
                   WHERE coalesce(s.text, d.text) LIKE ? LIMIT ?""",
                ("%" + RUNTIME_CONTEXT_TAG + "%", max(1, min(limit, 10_000))),
            ).fetchall()
        repaired = 0
        for row in rows:
            current = row["projection"] if row["projection"] is not None else row["raw_text"]
            payload = json.loads(gzip.decompress(row["payload"]))
            cleaned = projection_text(payload)
            if cleaned == current:
                continue
            with self.db() as db:
                db.execute("UPDATE document_search SET text=? WHERE id=?", (cleaned, row["id"]))
                db.execute("UPDATE outbox SET synced=NULL WHERE id=?", (row["id"],))
            repaired += 1
        return {"scanned": len(rows), "repaired": repaired}

    def pending(self, limit: int = 50) -> list[dict[str, Any]]:
        """Queue for the remote projection, with the current text and supersede flag."""
        with self.db() as db:
            return [dict(row) for row in db.execute("""
                SELECT d.id,d.namespace,d.source,d.item_key,d.payload,d.created,
                       coalesce(document_search.text,d.text) AS text,
                       EXISTS(SELECT 1 FROM document_versions v
                              WHERE v.id=d.id AND v.superseded IS NOT NULL) AS superseded
                FROM documents d JOIN outbox o ON d.id=o.id
                LEFT JOIN document_search ON document_search.id=d.id
                WHERE o.synced IS NULL AND d.namespace=? AND d.visibility=? ORDER BY d.created LIMIT ?
            """, (self.namespace, HOT, limit))]

    def set_projection(self, identifier: str, text: str) -> None:
        """Replace a rebuildable search view; the raw record and its payload stay intact."""
        with self.db() as db:
            found = db.execute("SELECT 1 FROM documents WHERE id=? AND namespace=?",
                               (identifier, self.namespace)).fetchone()
            if not found:
                raise ValueError("Memory record not found")
            db.execute("UPDATE document_search SET text=? WHERE id=?", (text, identifier))
            db.execute("UPDATE outbox SET synced=NULL WHERE id=?", (identifier,))

    def projection_candidates(self, source: str, marker: str, limit: int = 200) -> list[str]:
        """Identifiers of one source whose indexed text still starts with ``marker``."""
        with self.db() as db:
            rows = db.execute("""
                SELECT document_search.id FROM document_search JOIN documents d
                ON d.id=document_search.id
                WHERE d.namespace=? AND d.source=? AND document_search.text LIKE ? LIMIT ?
            """, (self.namespace, source, marker + "%", max(1, min(limit, 1000)))).fetchall()
        return [row[0] for row in rows]

    def mark_synced(self, identifiers: list[str]) -> None:
        with self.db() as db:
            db.executemany("UPDATE outbox SET synced=? WHERE id=?",
                           [(utcnow(), identifier) for identifier in identifiers])

    def account(self, identifier: str) -> Account:
        with self.db() as db:
            row = db.execute("SELECT encrypted FROM accounts WHERE id=? AND namespace=?",
                             (identifier, self.namespace)).fetchone()
        if row is None:
            raise ValueError("Account not found")
        return Account.model_validate_json(self._cipher.decrypt(row[0]))

    def accounts(self) -> list[Account]:
        with self.db() as db:
            rows = db.execute("SELECT encrypted FROM accounts WHERE namespace=? ORDER BY id",
                              (self.namespace,)).fetchall()
        return [Account.model_validate_json(self._cipher.decrypt(row[0])) for row in rows]

    def save_account(self, account: Account) -> dict[str, object]:
        data = account.model_dump()
        data["password"] = account.password.get_secret_value()
        data["smtp_password"] = account.smtp_password.get_secret_value()
        encrypted = self._cipher.encrypt(canonical(data))
        with self.db() as db:
            existing = db.execute("SELECT namespace FROM accounts WHERE id=?", (account.id,)).fetchone()
            if existing and existing[0] != self.namespace:
                raise ValueError("Account identity is in use")
            db.execute("INSERT OR REPLACE INTO accounts VALUES (?,?,?,?)",
                       (account.id, self.namespace, encrypted, utcnow()))
        return account.public()

    def checkpoint(self, key: str, default: str = "") -> str:
        with self.db() as db:
            row = db.execute("SELECT value FROM checkpoints WHERE namespace=? AND key=?",
                             (self.namespace, key)).fetchone()
        return row[0] if row else default

    def set_checkpoint(self, key: str, value: str) -> None:
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?)",
                       (self.namespace, key, value))

    def log_evolution(self, status: str, payload: Mapping[str, object]) -> None:
        with self.db() as db:
            db.execute("INSERT INTO evolution(namespace,created,status,payload) VALUES (?,?,?,?)",
                       (self.namespace, utcnow(), status, json.dumps(dict(payload))))

    def status(self) -> dict[str, Any]:
        with self.db() as db:
            count = db.execute("SELECT count(*),coalesce(sum(length(payload)),0) FROM documents WHERE namespace=?",
                               (self.namespace,)).fetchone()
            pending = db.execute(
                "SELECT count(*) FROM outbox o JOIN documents d ON d.id=o.id "
                "WHERE o.synced IS NULL AND d.namespace=? AND d.visibility=?",
                (self.namespace, HOT)).fetchone()[0]
            layers = {row[0]: row[1] for row in db.execute(
                "SELECT visibility,count(*) FROM documents WHERE namespace=? GROUP BY 1",
                (self.namespace,)).fetchall()}
            snapshots = db.execute("SELECT count(*) FROM snapshots WHERE namespace=?",
                                   (self.namespace,)).fetchone()[0]
            evolution = [{"id": r["id"], "created": r["created"], "status": r["status"],
                          "detail": json.loads(r["payload"])} for r in db.execute(
                "SELECT * FROM evolution WHERE namespace=? ORDER BY id DESC LIMIT 20", (self.namespace,))]
        evidence = 0
        if self.evidence_path.is_file():
            with self._connect(self.evidence_path) as cold:
                evidence = cold.execute("SELECT count(*) FROM documents").fetchone()[0]
        return {"documents": count[0], "compressed_bytes": count[1], "pending": pending,
                "snapshots": snapshots, "layers": layers, "evidence_documents": evidence,
                "evolution": evolution}
