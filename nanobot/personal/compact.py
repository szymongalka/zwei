"""Deterministic archive compaction: keep the corpus, preserve the evidence.

The live archive holds every raw record, but only the curated *hot* layer is
memory (mail, contacts, calendar, the owner's own turns).  Measured on
2026-09-17, session transcripts and workspace-file copies were 97% of the
documents and 90% of the characters, which is what made relevance on that corpus
a coin flip.

``plan`` reports the split without writing anything.  ``apply`` appends every
non-hot record to an evidence archive and rebuilds the live file so it holds
exactly the hot layer plus the archive metadata; records stay readable by
identifier, nothing is deleted, and a later run appends only what is new.
``resync`` drops the remote vector projection and rebuilds it from the curated
layer, so transcript noise can never come back through the remote index.

Both steps are deterministic: no model is involved, every count is checked before
the swap, and the swap itself is a single ``os.replace`` of the live file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.personal.service import PersonalService
from nanobot.personal.store import (
    ARCHIVE_METADATA_TABLES,
    ARCHIVE_SCHEMA,
    PersonalStore,
)
from nanobot.personal.visibility import HOT

HOT_STAGING = "archive.hot.new.sqlite3"
DOCUMENT_COLUMNS = "id,namespace,source,item_key,payload,text,created,visibility"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _open(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def _query(path: Path, statement: str, parameters: Sequence[object] = ()) -> list[sqlite3.Row]:
    db = _open(path)
    try:
        return db.execute(statement, parameters).fetchall()
    finally:
        db.close()


def _integrity(path: Path) -> str:
    return str(_query(path, "PRAGMA integrity_check")[0][0])


def _layers(path: Path) -> dict[str, int]:
    return {row["visibility"]: row["count"] for row in _query(
        path, "SELECT visibility,count(*) AS count FROM documents GROUP BY 1")}


def _checkpoint_and_verify(path: Path) -> dict[str, int]:
    db = _open(path)
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Live archive failed its integrity check: {integrity}")
    finally:
        db.close()
    return _layers(path)


def _records(path: Path, *, hot: bool) -> list[sqlite3.Row]:
    clause = "visibility=?" if hot else "visibility<>?"
    return _query(path, f"SELECT {DOCUMENT_COLUMNS} FROM documents WHERE {clause}", (HOT,))


def _insert_records(db: sqlite3.Connection, records: Sequence[sqlite3.Row]) -> int:
    """Insert records the target does not have yet, with their search projections."""
    known = {row[0] for row in db.execute("SELECT id FROM documents").fetchall()}
    fresh = [row for row in records if row["id"] not in known]
    db.executemany(f"INSERT OR IGNORE INTO documents VALUES ({','.join('?' * 8)})",
                   [tuple(row) for row in fresh])
    db.executemany("INSERT INTO document_search(id,text) VALUES (?,?)",
                   [(row["id"], row["text"]) for row in fresh])
    return len(fresh)


def _copy_table(db: sqlite3.Connection, source: Path, table: str) -> None:
    columns = [row[1] for row in _query(source, f"PRAGMA table_info({table})")]
    if not columns:
        return
    rows = [tuple(row) for row in _query(source, f"SELECT * FROM {table}")]
    db.executemany(
        f"INSERT OR IGNORE INTO {table} VALUES ({','.join('?' * len(columns))})", rows)


def _build_hot(target: Path, source: Path, records: Sequence[sqlite3.Row]) -> None:
    """Rebuild the live file: hot records, all metadata, and a fresh outbox."""
    target.unlink(missing_ok=True)
    db = _open(target)
    try:
        db.executescript(ARCHIVE_SCHEMA)
        db.execute("PRAGMA journal_mode=WAL")
        _insert_records(db, records)
        for table in ARCHIVE_METADATA_TABLES:
            _copy_table(db, source, table)
        db.execute("INSERT OR IGNORE INTO outbox(id) SELECT id FROM documents")
        db.commit()
    finally:
        db.close()


def _append_evidence(target: Path, source: Path, records: Sequence[sqlite3.Row]) -> dict[str, Any]:
    """Append non-hot records to the evidence archive, which is append-only too."""
    existed = target.is_file()
    db = _open(target)
    try:
        db.executescript(ARCHIVE_SCHEMA)
        db.execute("PRAGMA journal_mode=WAL")
        added = _insert_records(db, records)
        identifiers = {row["id"] for row in records}
        if identifiers:
            marks = ",".join("?" for _ in identifiers)
            versions = [tuple(row) for row in _query(
                source, f"SELECT * FROM document_versions WHERE id IN ({marks})",
                sorted(identifiers))]
            db.executemany(
                "INSERT OR IGNORE INTO document_versions VALUES (?,?,?,?,?,?)", versions)
        db.commit()
    finally:
        db.close()
    os.chmod(target, 0o600)
    return {"created": int(not existed), "path": str(target), "added": added}


def _sample_digests(path: Path, identifiers: Sequence[str]) -> dict[str, str]:
    if not identifiers:
        return {}
    marks = ",".join("?" for _ in identifiers)
    return {row["id"]: hashlib.sha256(row["payload"]).hexdigest() for row in _query(
        path, f"SELECT id,payload FROM documents WHERE id IN ({marks})", list(identifiers))}


def _verify(target: Path, expected: set[str], label: str) -> None:
    integrity = _integrity(target)
    if integrity != "ok":
        raise RuntimeError(f"{label} failed its integrity check: {integrity}")
    present = {row[0] for row in _query(target, "SELECT id FROM documents")}
    missing = expected - present
    if missing:
        raise RuntimeError(f"{label} is missing {len(missing)} of {len(expected)} records")


def _verify_against(target: Path, origin: Path, identifiers: set[str]) -> None:
    sample = sorted(identifiers)[:25]
    if not sample:
        return
    if _sample_digests(target, sample) != _sample_digests(origin, sample):
        raise RuntimeError("Rebuilt records do not match the pre-compaction bytes")


def plan(store: PersonalStore) -> dict[str, Any]:
    layers = _layers(store.path)
    return {
        "archive": str(store.path),
        "bytes": store.path.stat().st_size,
        "integrity": _integrity(store.path),
        "documents": sum(layers.values()),
        "layers": layers,
        "pending_hot": store.status()["pending"],
        "evidence_archive": str(store.evidence_path),
        "evidence_exists": store.evidence_path.is_file(),
        "would_keep": layers.get(HOT, 0),
        "would_move": sum(count for layer, count in layers.items() if layer != HOT),
    }


def apply(store: PersonalStore, *, stamp: str | None = None) -> dict[str, Any]:
    """Move non-hot records to evidence and rebuild the live file around the hot layer."""
    stamp = stamp or _now()
    source = store.path
    evidence = store.evidence_path
    staging = store.directory / HOT_STAGING
    layers = _checkpoint_and_verify(source)
    bytes_before = source.stat().st_size
    hot_records = _records(source, hot=True)
    other_records = _records(source, hot=False)
    logger.info("Compaction plan: {} hot, {} evidence records", len(hot_records), len(other_records))

    evidence_report = _append_evidence(evidence, source, other_records)
    _build_hot(staging, source, hot_records)
    _verify(staging, {row["id"] for row in hot_records}, "Staged archive")
    _verify_against(staging, source, {row["id"] for row in hot_records})
    if other_records:
        _verify(evidence, {row["id"] for row in other_records}, "Evidence archive")
        _verify_against(evidence, source, {row["id"] for row in other_records})

    for suffix in ("-wal", "-shm"):
        Path(str(source) + suffix).unlink(missing_ok=True)
    os.replace(staging, source)
    store.set_checkpoint("remote_state", "pending")
    store.set_checkpoint("archive_evidence", evidence.name)
    result = {
        "stamp": stamp,
        "before": layers,
        "kept": len(hot_records),
        "moved": len(other_records),
        "bytes_before": bytes_before,
        "bytes_after": source.stat().st_size,
        "evidence": evidence_report,
        "integrity": _integrity(source),
    }
    logger.info("Compaction done: live archive {} bytes, {} records", result["bytes_after"],
                result["kept"])
    return result


def resync(service: PersonalService, *, max_batches: int = 400) -> dict[str, int]:
    """Rebuild the remote vector projection from the curated layer alone."""
    vector = service.vector
    if vector is None:
        raise RuntimeError("Remote memory is not configured")
    purged = vector.purge()
    store = service.store
    with store.db() as db:
        db.execute("DELETE FROM outbox")
        db.execute("INSERT INTO outbox(id) SELECT id FROM documents WHERE namespace=? AND visibility=?",
                   (store.namespace, HOT))
        db.execute("UPDATE snapshot_outbox SET synced=CURRENT_TIMESTAMP")
    synced = 0
    for _ in range(max_batches):
        if store.status()["pending"] == 0:
            break
        try:
            synced += int(service.sync_memory()["synced"])
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            logger.warning("Remote projection batch skipped ({})", type(exc).__name__)
            break
    return {"purged_chunks": purged["chunks"], "purged_documents": purged["documents"],
            "synced": synced, "pending": int(store.status()["pending"])}


def _service(config_path: Path) -> PersonalService:
    from nanobot.config.loader import load_config
    config = load_config(config_path)
    return PersonalService(config.personal, config.workspace_path, None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic personal archive compaction")
    parser.add_argument("command", choices=("plan", "apply", "resync"))
    parser.add_argument("--config", default=str(Path.home() / ".nanobot" / "config.json"))
    parser.add_argument("--confirm", action="store_true",
                        help="required for apply: the live archive file is replaced")
    options = parser.parse_args(argv)

    service = _service(Path(options.config))
    if options.command == "plan":
        print(json.dumps(plan(service.store), indent=1, ensure_ascii=False))
        return 0
    if options.command == "apply":
        if not options.confirm:
            print("apply replaces the live archive file; pass --confirm", file=sys.stderr)
            return 2
        print(json.dumps(apply(service.store), indent=1, ensure_ascii=False))
        return 0
    print(json.dumps(resync(service), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
