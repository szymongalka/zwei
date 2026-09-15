"""Move only durably archived INBOX messages using targeted IMAP UID operations."""

from __future__ import annotations

import hashlib
import imaplib
import json

from nanobot.personal.config import Account
from nanobot.personal.store import PersonalStore, canonical, utcnow


def quote_mailbox(name: str) -> str:
    import base64
    # IMAP modified UTF-7 preserves user-defined Polish folder names.
    output: list[str] = []
    pending: list[str] = []
    def flush() -> None:
        if pending:
            output.append("&" + base64.b64encode("".join(pending).encode("utf-16-be")).decode().rstrip("=").replace("/", ",") + "-")
            pending.clear()
    for char in name:
        if 32 <= ord(char) <= 126:
            flush()
            output.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    flush()
    return '"' + "".join(output).replace("\\", "\\\\").replace('"', '\\"') + '"'


def move_archived_mail(store: PersonalStore, account: Account, client: imaplib.IMAP4_SSL,
                       identifier: str, source: str, generation: str, uid: int) -> None:
    # Require the durable raw record before allowing any mutation of the source mailbox.
    record = store.get(identifier)
    if record["source"] != "mail:" + account.id:
        raise ValueError("Archive does not belong to this account")
    with store.db() as db:
        row = db.execute("SELECT category FROM inbox WHERE id=? AND namespace=?", (identifier, store.namespace)).fetchone()
    if row is None:
        raise ValueError("Archived message has no category")
    rule = next((r for r in account.folder_rules if r.id == row["category"]), None)
    if rule is None:
        return
    target = rule.folder
    if source == target:
        return
    operation_id = "move-" + hashlib.sha256(canonical([store.namespace, account.id, source, generation, uid, target])).hexdigest()
    capabilities = {v.decode("ascii").upper() if isinstance(v, bytes) else v.upper() for v in client.capabilities}
    if "MOVE" not in capabilities and "UIDPLUS" not in capabilities:
        store.set_checkpoint("account_sort:" + account.id, "unsupported:MOVE_or_UIDPLUS_required")
        return
    with store.db() as db:
        previous = db.execute("SELECT status FROM operations WHERE id=?", (operation_id,)).fetchone()
        if previous:
            if previous[0] == "moved":
                return
            if previous[0] != "preparing":
                store.set_checkpoint("account_sort:" + account.id, "uncertain:" + operation_id)
                return
        db.execute("INSERT OR IGNORE INTO operations VALUES (?,?,?,?,?,?)", (
            operation_id, store.namespace, "imap_move", "preparing",
            json.dumps({"record": identifier, "source": source, "uid": uid, "uidvalidity": generation, "target": target}), utcnow()))
    status, existing = client.list('""', quote_mailbox(target))
    if status != "OK":
        raise ValueError("Cannot inspect destination folder")
    if not any(existing or []):
        status, _ = client.create(quote_mailbox(target))
        if status != "OK":
            raise ValueError("Cannot create sorting folder")
    with store.db() as db:
        db.execute("UPDATE operations SET status='moving' WHERE id=?", (operation_id,))
    try:
        if "MOVE" in capabilities:
            status, _ = client.uid("MOVE", str(uid), quote_mailbox(target))
            if status != "OK":
                raise ValueError("IMAP MOVE failed")
        else:
            status, _ = client.uid("COPY", str(uid), quote_mailbox(target))
            if status != "OK":
                raise ValueError("IMAP COPY failed")
            status, _ = client.uid("STORE", str(uid), "+FLAGS.SILENT", "(\\Deleted)")
            if status != "OK":
                raise ValueError("IMAP source flag update failed")
            # Never use global EXPUNGE: it could remove unrelated user-deleted messages.
            status, _ = client.uid("EXPUNGE", str(uid))
            if status != "OK":
                raise ValueError("IMAP UID EXPUNGE failed")
    except Exception:
        with store.db() as db:
            db.execute("UPDATE operations SET status='uncertain' WHERE id=?", (operation_id,))
        store.set_checkpoint("account_sort:" + account.id, "uncertain:" + operation_id)
        raise
    with store.db() as db:
        db.execute("UPDATE operations SET status='moved' WHERE id=?", (operation_id,))
    store.set_checkpoint("account_sort:" + account.id, "ready")
