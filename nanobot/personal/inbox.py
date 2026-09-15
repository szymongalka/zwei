"""Rebuildable, deduplicated inbox view; original mail stays in the raw archive."""

from __future__ import annotations

import hashlib
import re
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any

from nanobot.personal.store import PersonalStore, canonical, utcnow


def project_mail(store: PersonalStore, identifier: str, payload: dict[str, Any]) -> None:
    headers = payload["headers"]
    subject = str(headers.get("Subject", ""))
    sender = str(headers.get("From", ""))
    body = str(payload.get("body", ""))
    context = sender + "\n" + subject + "\n" + body[:12000]
    account = store.account(payload["account_id"])
    category = next((rule.id for rule in account.folder_rules
                     if any(word.casefold() in context.casefold() for word in rule.contains if word)
                     or any(value.casefold() in sender.casefold() for value in rule.senders if value)), "other")
    priority = 0
    try:
        parsed = parsedate_to_datetime(str(headers.get("Date", "")))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        sent_at = parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        sent_at = utcnow()
    identity = [headers.get("Message-ID", ""), headers.get("Date", ""), sender, subject, body, payload.get("attachments", [])]
    logical_key = hashlib.sha256(canonical(identity)).hexdigest()
    with store.db() as db:
        db.execute("INSERT OR IGNORE INTO inbox VALUES (?,?,?,?,?,?,?,?,?,?)", (
            identifier, store.namespace, payload["account_id"], logical_key, sender, subject,
            sent_at, category, priority, re.sub(r"\s+", " ", body)[:350]))


def list_inbox(store: PersonalStore, account_ids: list[str], category: str,
               offset: int, limit: int, sort: str = "newest") -> dict[str, Any]:
    order = {"newest": "sent_at DESC,id", "oldest": "sent_at ASC,id",
             "sender": "sender COLLATE NOCASE,sent_at DESC,id"}.get(sort)
    if order is None:
        raise ValueError("Unsupported inbox sort")
    available = {a.id for a in store.accounts() if a.enabled and a.mail_enabled and a.include_inbox}
    selected = sorted(available.intersection(account_ids) if account_ids else available)
    if not selected:
        return {"messages": [], "total": 0, "next_offset": None}
    placeholders = ",".join("?" for _ in selected)
    where = f"namespace=? AND account_id IN ({placeholders})"
    parameters: list[object] = [store.namespace, *selected]
    if category:
        where += " AND category=?"
        parameters.append(category)
    with store.db() as db:
        total = db.execute(f"SELECT count(DISTINCT logical_key) FROM inbox WHERE {where}", parameters).fetchone()[0]
        rows = db.execute(f"""SELECT * FROM (
            SELECT *,row_number() OVER(PARTITION BY logical_key ORDER BY sent_at DESC,id) AS position
            FROM inbox WHERE {where}) WHERE position=1
            ORDER BY {order} LIMIT ? OFFSET ?""", [*parameters, limit, offset]).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            copies = db.execute(f"SELECT id,account_id FROM inbox WHERE {where} AND logical_key=?",
                                [*parameters, row["logical_key"]]).fetchall()
            result.append({**{k: row[k] for k in ("id", "sender", "subject", "sent_at", "category", "priority", "preview")},
                           "copies": [dict(c) for c in copies]})
    return {"messages": result, "total": total, "next_offset": offset + limit if offset + limit < total else None}


def categorize(store: PersonalStore, identifier: str, category: str) -> dict[str, object]:
    allowed = {"other", *(rule.id for account in store.accounts() for rule in account.folder_rules)}
    if category not in allowed:
        raise ValueError("Unknown category")
    with store.db() as db:
        row = db.execute("SELECT logical_key FROM inbox WHERE id=? AND namespace=?",
                         (identifier, store.namespace)).fetchone()
        if row is None:
            raise ValueError("Inbox message not found")
        db.execute("UPDATE inbox SET category=? WHERE logical_key=? AND namespace=?",
                   (category, row[0], store.namespace))
    store.put("inbox_correction", identifier, {"category": category, "corrected_at": utcnow()})
    return {"id": identifier, "category": category}
