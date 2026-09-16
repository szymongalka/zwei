"""Remote archive and rebuildable multilingual pgvector search projection."""

from __future__ import annotations

import gzip
import json
import threading
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastembed import TextEmbedding
    from psycopg import Connection

from nanobot.personal.store import PersonalStore

_EMBEDDING_LOCK = threading.RLock()


@lru_cache(maxsize=2)
def _embedding_model(name: str, cache: str) -> TextEmbedding:
    from fastembed import TextEmbedding
    return TextEmbedding(name, cache_dir=cache, threads=2)


def chunks(text: str, size: int = 1600, overlap: int = 160) -> list[str]:
    if size < 100 or not 0 <= overlap < size:
        raise ValueError("Invalid chunk policy")
    return [text[start:start + size] for start in range(0, len(text), size - overlap)] or [""]


class VectorMemory:
    def __init__(self, store: PersonalStore, connection_file: Path, model: str):
        self.store = store
        self.connection_file = connection_file
        self.model_name = model
        self._model: TextEmbedding | None = None
        self._initialized = False

    def connect(self) -> Connection[tuple[Any, ...]]:
        import psycopg
        config = json.loads(self.connection_file.read_text())
        if config.get("sslmode") != "verify-full":
            raise ValueError("Memory database requires verified TLS")
        allowed = {"host", "hostaddr", "port", "dbname", "user", "password", "sslmode",
                   "sslrootcert", "connect_timeout"}
        if set(config) - allowed:
            raise ValueError("Unsupported memory connection option")
        return psycopg.connect(**config)

    def initialize(self) -> None:
        if self._initialized:
            return
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS personal_documents (
                id text PRIMARY KEY, namespace text NOT NULL, source text NOT NULL,
                item_key text NOT NULL, payload bytea NOT NULL, created timestamptz NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS personal_chunks (
                id text NOT NULL REFERENCES personal_documents(id), ordinal integer NOT NULL,
                namespace text NOT NULL, model text NOT NULL, content text NOT NULL,
                embedding vector(384) NOT NULL, PRIMARY KEY(id,ordinal,model)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS personal_chunks_namespace ON personal_chunks(namespace)")
            db.execute("CREATE INDEX IF NOT EXISTS personal_chunks_vector ON personal_chunks USING hnsw (embedding vector_cosine_ops)")
            db.execute("""CREATE TABLE IF NOT EXISTS personal_snapshots (
                id text PRIMARY KEY, namespace text NOT NULL, session_key text NOT NULL,
                reason text NOT NULL, document_ids bytea NOT NULL, created timestamptz NOT NULL
            )""")
        self._initialized = True

    def embed(self, values: list[str]) -> list[list[float]]:
        with _EMBEDDING_LOCK:
            if self._model is None:
                self._model = _embedding_model(self.model_name, str(self.store.directory / "models"))
            result = [value.tolist() for value in self._model.embed(values, batch_size=16)]
            if any(len(value) != 384 for value in result):
                raise ValueError("This archive requires a 384-dimensional embedding model")
            return result

    def sync(self, batch_size: int = 50) -> int:
        self.initialize()
        pending = self.store.pending(batch_size)
        for document in pending:
            if document.get("superseded"):
                # A newer version of the same item owns the projection; the archive keeps
                # this copy, but the rebuildable remote index must not return both.
                self._drop_superseded(document["id"])
                self.store.mark_synced([document["id"]])
                continue
            parts = chunks(document["text"])
            embeddings = self.embed(parts)
            with self.connect() as db:
                db.execute("""INSERT INTO personal_documents VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING""", tuple(document[k] for k in (
                        "id", "namespace", "source", "item_key", "payload", "created")))
                for index, (part, embedding) in enumerate(zip(parts, embeddings, strict=True)):
                    db.execute("""INSERT INTO personal_chunks VALUES (%s,%s,%s,%s,%s,%s::vector)
                        ON CONFLICT (id,ordinal,model) DO NOTHING""", (
                            document["id"], index, document["namespace"], self.model_name,
                            part, json.dumps(embedding)))
            # Acknowledgement follows the remote commit; crashes safely repeat the insert.
            self.store.mark_synced([document["id"]])
        with self.store.db() as local:
            snapshots = local.execute("""SELECT s.* FROM snapshots s JOIN snapshot_outbox o
                ON s.id=o.id WHERE s.namespace=? AND o.synced IS NULL LIMIT ?""",
                (self.store.namespace, batch_size)).fetchall()
        for row in snapshots:
            identifiers = json.loads(gzip.decompress(row["document_ids"]))
            with self.store.db() as local:
                if any(local.execute("SELECT 1 FROM outbox WHERE id=? AND synced IS NULL",
                                     (identifier,)).fetchone() for identifier in identifiers):
                    continue
            with self.connect() as remote:
                remote.execute("""INSERT INTO personal_snapshots VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING""", tuple(row))
            with self.store.db() as local:
                local.execute("UPDATE snapshot_outbox SET synced=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
        return len(pending)

    def _drop_superseded(self, identifier: str) -> None:
        """Remove the rebuildable projection of one archived version from the remote index."""
        with self.connect() as db:
            db.execute("DELETE FROM personal_chunks WHERE id=%s AND namespace=%s",
                       (identifier, self.store.namespace))
            db.execute("DELETE FROM personal_documents WHERE id=%s AND namespace=%s",
                       (identifier, self.store.namespace))

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        self.initialize()
        vector = self.embed([query])[0]
        with self.connect() as db:
            rows = db.execute("""SELECT c.id,d.source,d.item_key,c.content,d.created,
                c.embedding <=> %s::vector AS distance
                FROM personal_chunks c JOIN personal_documents d ON d.id=c.id
                WHERE c.namespace=%s AND c.model=%s
                ORDER BY distance LIMIT %s""", (
                    json.dumps(vector), self.store.namespace, self.model_name, min(limit * 3, 150),
                )).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for identifier, source, key, content, created, distance in rows:
            if identifier not in result:
                result[identifier] = {"id": identifier, "source": source, "key": key,
                                      "excerpt": content, "created": created.isoformat(),
                                      "distance": distance}
        return list(result.values())[:limit]
