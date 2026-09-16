"""Durable local archive and outbox; raw records are independent of search projections."""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import os
import re
import sqlite3
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from cryptography.fernet import Fernet
from filelock import FileLock

from nanobot.personal.config import Account


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


class PersonalStore:
    def __init__(self, directory: Path, workspace: Path):
        self.directory = directory.expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.namespace = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:24]
        self.path = self.directory / "archive.sqlite3"
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
            db.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, namespace TEXT NOT NULL, source TEXT NOT NULL,
                    item_key TEXT NOT NULL, payload BLOB NOT NULL, text TEXT NOT NULL,
                    created TEXT NOT NULL
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
            """)
        self.path.chmod(0o600)

    @contextmanager
    def db(self) -> Generator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _put(self, db: sqlite3.Connection, source: str, key: str, payload: object,
             text: str | None = None, version: str | None = None) -> str:
        raw = canonical(payload)
        identity = canonical([self.namespace, source, key]) + raw
        identifier = hashlib.sha256(identity).hexdigest()
        projection = searchable_text(payload) if text is None else text
        inserted = db.execute(
            "INSERT OR IGNORE INTO documents VALUES (?,?,?,?,?,?,?)",
            (identifier, self.namespace, source, key, gzip.compress(raw, mtime=0), projection, utcnow()),
        ).rowcount
        if inserted:
            db.execute("INSERT INTO document_search VALUES (?,?)", (identifier, projection))
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
            identifiers = [self._put(db, "session:" + session_key, str(index), message)
                           for index, message in enumerate(messages)]
            raw = canonical(identifiers)
            identity = hashlib.sha256(canonical([self.namespace, session_key, reason]) + raw).hexdigest()
            db.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?,?)", (
                identity, self.namespace, session_key, reason, gzip.compress(raw, mtime=0), utcnow(),
            ))
            db.execute("INSERT OR IGNORE INTO snapshot_outbox(id) VALUES (?)", (identity,))
        return identity

    def get(self, identifier: str) -> dict[str, Any]:
        with self.db() as db:
            row = db.execute("SELECT * FROM documents WHERE id=? AND namespace=?",
                             (identifier, self.namespace)).fetchone()
        if row is None:
            raise ValueError("Memory record not found")
        return {"id": row["id"], "source": row["source"], "key": row["item_key"],
                "created": row["created"], "payload": json.loads(gzip.decompress(row["payload"]))}

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        words = re.findall(r"\w+", query, re.UNICODE)[:20]
        if not words:
            return []
        expression = " OR ".join('"' + word + '"' for word in words)
        with self.db() as db:
            rows = db.execute("""
                SELECT d.id,d.source,d.item_key,document_search.text,d.created
                FROM document_search JOIN documents d ON d.id=document_search.id
                WHERE document_search MATCH ? AND d.namespace=?
                  AND d.id NOT IN (SELECT id FROM document_versions WHERE superseded IS NOT NULL)
                ORDER BY bm25(document_search) LIMIT ?
            """, (expression, self.namespace, max(1, min(limit, 100)))).fetchall()
        return [{"id": r["id"], "source": r["source"], "key": r["item_key"],
                 "excerpt": r["text"][:1800], "created": r["created"]} for r in rows]

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
                WHERE o.synced IS NULL AND d.namespace=? ORDER BY d.created LIMIT ?
            """, (self.namespace, limit))]

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

    def status(self) -> dict[str, object]:
        with self.db() as db:
            count = db.execute("SELECT count(*),coalesce(sum(length(payload)),0) FROM documents WHERE namespace=?",
                               (self.namespace,)).fetchone()
            pending = db.execute("SELECT count(*) FROM outbox o JOIN documents d ON d.id=o.id WHERE o.synced IS NULL AND d.namespace=?",
                                 (self.namespace,)).fetchone()[0]
            snapshots = db.execute("SELECT count(*) FROM snapshots WHERE namespace=?",
                                   (self.namespace,)).fetchone()[0]
            evolution = [{"id": r["id"], "created": r["created"], "status": r["status"],
                          "detail": json.loads(r["payload"])} for r in db.execute(
                "SELECT * FROM evolution WHERE namespace=? ORDER BY id DESC LIMIT 20", (self.namespace,))]
        return {"documents": count[0], "compressed_bytes": count[1], "pending": pending,
                "snapshots": snapshots, "evolution": evolution}
