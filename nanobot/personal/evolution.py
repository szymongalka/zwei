"""Autonomous, reversible retrieval experiments over held-out archive examples.

This optimizes retrieval policy, not the weights of a hosted model. The existing
Dream mechanism continues to own profile, memory and skill consolidation.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING

from filelock import FileLock

from nanobot.personal.store import PersonalStore, canonical

if TYPE_CHECKING:
    from nanobot.personal.service import PersonalService


def examples(store: PersonalStore, limit: int = 120) -> list[tuple[str, str]]:
    with store.db() as db:
        rows = db.execute("""SELECT id,text FROM documents WHERE namespace=? AND length(text)>80
            ORDER BY created DESC LIMIT ?""", (store.namespace, limit * 3)).fetchall()
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        # Self-supervised source-retrieval probes; never presented as human feedback.
        words = [w for w in re.findall(r"[^\W\d_]{4,}", row["text"], re.UNICODE)
                 if w.lower() not in {"content", "assistant", "user", "role", "tool", "system"}]
        if len(words) < 8:
            continue
        query = " ".join(words[:8])
        if query.casefold() in seen:
            continue
        seen.add(query.casefold())
        result.append((query, row["id"]))
        if len(result) >= limit:
            break
    return sorted(result, key=lambda item: hashlib.sha256(item[1].encode()).hexdigest())


def evaluate(service: PersonalService, samples: list[tuple[str, str]], policy: str) -> float:
    scores: list[float] = []
    for query, target in samples:
        found = service.search(query, 8, policy=policy)
        rank = next((i + 1 for i, record in enumerate(found) if record["id"] == target), None)
        scores.append(1 / rank if rank else 0)
    return sum(scores) / len(scores) if scores else 0


def evolve(service: PersonalService) -> dict[str, object]:
    store = service.store
    with FileLock(str(store.directory / "evolution.lock"), timeout=0):
        samples = examples(store)
        signature = hashlib.sha256(canonical(samples)).hexdigest()
        if len(samples) < service.config.evolution_min_samples:
            result: dict[str, object] = {"reason": "insufficient_data", "samples": len(samples),
                                         "required": service.config.evolution_min_samples}
            if store.checkpoint("evolution_dataset") != signature:
                store.log_evolution("waiting", result)
                store.set_checkpoint("evolution_dataset", signature)
            return result
        if store.checkpoint("evolution_dataset") == signature:
            return {"reason": "dataset_unchanged", "samples": len(samples)}
        train = samples[::2]
        holdout = samples[1::2]
        current = store.checkpoint("retrieval_policy", "hybrid")
        policies = [current] + [p for p in ("lexical", "hybrid", "semantic") if p != current]
        if service.vector is None or store.checkpoint("remote_state") != "ready":
            return {"reason": "remote_memory_not_ready"}
        candidates = policies[:service.config.evolution_max_trials]
        scores = {p: evaluate(service, train, p) for p in candidates}
        candidate = max(scores, key=lambda p: scores[p])
        previous_score = evaluate(service, holdout, current)
        candidate_score = evaluate(service, holdout, candidate)
        promote = (candidate != current and scores[candidate] > scores[current] + 0.01
                   and candidate_score >= previous_score + 0.01)
        result = {"metric": "self_supervised_source_retrieval_mrr_at_8", "dataset": signature,
                  "train_samples": len(train), "holdout_samples": len(holdout),
                  "train_scores": scores, "previous": current, "candidate": candidate,
                  "previous_holdout": previous_score, "candidate_holdout": candidate_score,
                  "promoted": promote}
        # One transaction records evidence and changes the active policy together.
        with store.db() as db:
            db.execute("INSERT INTO evolution(namespace,created,status,payload) VALUES (?,CURRENT_TIMESTAMP,?,?)",
                       (store.namespace, "promoted" if promote else "retained", json.dumps(result)))
            if promote:
                db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?)",
                           (store.namespace, "retrieval_previous", current))
                db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?)",
                           (store.namespace, "retrieval_policy", candidate))
            db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?)",
                       (store.namespace, "evolution_dataset", signature))
        return result


def rollback(store: PersonalStore) -> dict[str, object]:
    with FileLock(str(store.directory / "evolution.lock"), timeout=0):
        previous = store.checkpoint("retrieval_previous")
        if previous not in {"lexical", "hybrid", "semantic"}:
            raise ValueError("No previous retrieval policy is available")
        with store.db() as db:
            db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?)",
                       (store.namespace, "retrieval_policy", previous))
            db.execute("DELETE FROM checkpoints WHERE namespace=? AND key='retrieval_previous'", (store.namespace,))
            db.execute("INSERT INTO evolution(namespace,created,status,payload) VALUES (?,CURRENT_TIMESTAMP,?,?)",
                       (store.namespace, "rolled_back", json.dumps({"policy": previous})))
        return {"policy": previous}
