"""Mailbox/DAV ingestion, archived-first sorting and deduplicated SMTP delivery."""

from __future__ import annotations

import base64
import email
import hashlib
import imaplib
import json
import re
import smtplib
import ssl
from email import policy
from email.message import EmailMessage, Message
from email.utils import getaddresses, parseaddr
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

import httpx

from nanobot.personal.config import Account
from nanobot.personal.store import PersonalStore
from nanobot.security.network import pin_resolved_url_dns, resolve_url_target

DAV = "DAV:"
CAL = "urn:ietf:params:xml:ns:caldav"
CARD = "urn:ietf:params:xml:ns:carddav"


def checked_target(url: str) -> tuple[str, ...]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("A credential-free HTTPS endpoint is required")
    ok, error, addresses = resolve_url_target(url)
    if not ok or not addresses:
        raise ValueError("Endpoint blocked by network policy: " + error)
    return addresses


def _imap(account: Account) -> imaplib.IMAP4_SSL:
    url = f"https://{account.imap_host}:{account.imap_port}"
    addresses = checked_target(url)
    with pin_resolved_url_dns(url, addresses):
        client = imaplib.IMAP4_SSL(account.imap_host, account.imap_port,
                                   ssl_context=ssl.create_default_context(), timeout=30)
    try:
        client.login(account.username, account.password.get_secret_value())
    except Exception:
        client.logout()
        raise
    return client


def _smtp(account: Account) -> smtplib.SMTP:
    url = f"https://{account.smtp_host}:{account.smtp_port}"
    addresses = checked_target(url)
    context = ssl.create_default_context()
    with pin_resolved_url_dns(url, addresses):
        if account.smtp_security == "tls":
            client = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, context=context, timeout=30)
        else:
            client = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=30)
    try:
        client.ehlo()
        if account.smtp_security == "starttls":
            client.starttls(context=context)
            client.ehlo()
        password = account.smtp_password.get_secret_value() or account.password.get_secret_value()
        client.login(account.smtp_username or account.username, password)
    except Exception:
        client.close()
        raise
    return client


def _body(message: Message) -> str:
    parts = message.walk() if message.is_multipart() else [message]
    output: list[str] = []
    for part in parts:
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        raw = part.get_payload(decode=True)
        if not isinstance(raw, bytes):
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = raw.decode(charset, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        if part.get_content_type() == "text/html":
            text = re.sub(r"<[^>]+>", " ", text)
        output.append(text)
    return "\n".join(output)


def mailbox_sync(store: PersonalStore, account: Account, batch_size: int) -> int:
    from nanobot.personal.mail_sort import quote_mailbox
    imported = 0
    with _imap(account) as client:
        for folder in account.folders:
            status, _ = client.select(quote_mailbox(folder), readonly=not account.organize_folders)
            if status != "OK":
                raise ValueError("Mailbox selection failed")
            validity = client.response("UIDVALIDITY")[1]
            if not validity or validity[0] is None:
                raise ValueError("Server did not supply UIDVALIDITY")
            generation = bytes(validity[0]).decode("ascii")
            key = f"imap:{account.id}:{folder}:{generation}"
            cursor = int(store.checkpoint(key, "0"))
            status, found = client.uid("SEARCH", "UID", f"{cursor + 1}:*")
            if status != "OK":
                raise ValueError("Mailbox search failed")
            # A range past the newest UID matches nothing, and some servers then
            # omit the untagged SEARCH line, which imaplib reports as [None].
            listing = found[0] if found and isinstance(found[0], bytes) else b""
            uids = [int(uid) for uid in listing.split() if int(uid) > cursor]
            for uid in sorted(uids)[:batch_size]:
                status, sizes = client.uid("FETCH", str(uid), "(RFC822.SIZE)")
                size_text = b" ".join(item for item in sizes or [] if isinstance(item, bytes))
                size = re.search(rb"RFC822.SIZE\s+(\d+)", size_text)
                if status != "OK" or size is None:
                    raise ValueError("Cannot determine message size; cursor retained")
                if int(size[1]) > account.max_message_bytes:
                    raise ValueError(f"Message UID {uid} exceeds the account size limit; cursor retained")
                status, fetched = client.uid("FETCH", str(uid), "(BODY.PEEK[])")
                raw = next((item[1] for item in fetched or [] if isinstance(item, tuple)
                            and isinstance(item[1], bytes)), None)
                if status != "OK" or raw is None:
                    raise ValueError("Message fetch failed; cursor retained")
                if len(raw) > account.max_message_bytes:
                    raise ValueError("Fetched message exceeds size limit; cursor retained")
                message = email.message_from_bytes(raw, policy=policy.default)
                text = _body(message)
                headers = {name: str(message.get(name, "")) for name in (
                    "Subject", "From", "To", "Cc", "Date", "Message-ID", "In-Reply-To")}
                attachments: list[dict[str, str | None]] = []
                for part in message.walk():
                    if part.get_filename():
                        attachment = part.get_payload(decode=True)
                        attachments.append({"filename": part.get_filename(), "type": part.get_content_type(),
                                            "sha256": hashlib.sha256(attachment if isinstance(attachment, bytes) else part.as_bytes()).hexdigest()})
                payload = {"headers": headers, "body": text, "attachments": attachments,
                           "raw_rfc822_b64": base64.b64encode(raw).decode(), "folder": folder,
                           "uid": uid, "uidvalidity": generation, "account_id": account.id}
                identifier = store.put("mail:" + account.id, f"{folder}:{generation}:{uid}", payload,
                                       "\n".join(headers.values()) + "\n" + text)
                from nanobot.personal.inbox import project_mail
                project_mail(store, identifier, payload)
                if account.organize_folders and folder.upper() == "INBOX":
                    from nanobot.personal.mail_sort import move_archived_mail
                    move_archived_mail(store, account, client, identifier, folder, generation, uid)
                store.set_checkpoint(key, str(uid))
                imported += 1
    return imported


class DAVClient:
    def __init__(self, account: Account, base_url: str):
        self.account = account
        self.base_url = base_url
        checked_target(base_url)

    def target(self, current: str, href: str) -> str:
        target = urljoin(current, href)
        parsed = urlsplit(target)
        base = urlsplit(self.base_url)
        hostname = parsed.hostname or ""
        original = base.hostname or ""
        same_owner = (parsed.port or 443) == (base.port or 443) and (hostname == original or (
            self.account.kind == "apple" and hostname.endswith(".icloud.com")
            and original.endswith(".icloud.com")))
        if not same_owner:
            raise ValueError("DAV discovery crossed an account origin boundary")
        checked_target(target)
        return target

    def request(self, method: str, url: str, body: str = "", depth: str = "0") -> bytes:
        target = self.target(self.base_url, url)
        for _ in range(5):
            addresses = checked_target(target)
            with pin_resolved_url_dns(target, addresses), httpx.Client(
                timeout=30, trust_env=False, follow_redirects=False,
                auth=(self.account.username, self.account.password.get_secret_value()),
            ) as client:
                with client.stream(method, target, content=body.encode(), headers={
                    "Depth": depth, "Content-Type": "application/xml; charset=utf-8",
                }) as response:
                    if response.status_code in {301, 302, 307, 308}:
                        target = self.target(target, response.headers.get("location", ""))
                        continue
                    if response.status_code not in {200, 207}:
                        raise ValueError(f"DAV server returned HTTP {response.status_code}")
                    parts: list[bytes] = []
                    total = 0
                    for part in response.iter_bytes():
                        total += len(part)
                        if total > self.account.max_message_bytes:
                            raise ValueError("DAV response exceeds size limit")
                        parts.append(part)
                    return b"".join(parts)
        raise ValueError("Too many DAV redirects")

    def properties(self, url: str, properties: str, depth: str = "0") -> ET.Element:
        from defusedxml.ElementTree import fromstring
        body = f'<d:propfind xmlns:d="DAV:" xmlns:c="{CAL}" xmlns:a="{CARD}"><d:prop>{properties}</d:prop></d:propfind>'
        return fromstring(self.request("PROPFIND", url, body, depth))

    def collections(self, kind: str) -> list[str]:
        namespace = CAL if kind == "calendar" else CARD
        name = "calendar" if kind == "calendar" else "addressbook"
        tree = self.properties(self.base_url, "<d:current-user-principal/>")
        principal = tree.findtext(f".//{{{DAV}}}current-user-principal/{{{DAV}}}href")
        if not principal:
            raise ValueError("DAV principal discovery failed")
        principal_url = self.target(self.base_url, principal)
        prefix = "c" if kind == "calendar" else "a"
        tree = self.properties(principal_url, f"<{prefix}:{name}-home-set/>")
        home = tree.findtext(f".//{{{namespace}}}{name}-home-set/{{{DAV}}}href")
        if not home:
            raise ValueError("DAV home-set discovery failed")
        home_url = self.target(principal_url, home)
        tree = self.properties(home_url, "<d:resourcetype/><d:displayname/>", "1")
        return [self.target(home_url, node.findtext(f"{{{DAV}}}href", ""))
                for node in tree.findall(f"{{{DAV}}}response")
                if node.find(f".//{{{DAV}}}resourcetype/{{{namespace}}}{name}") is not None]


def dav_sync(store: PersonalStore, account: Account, kind: str, limit: int) -> int:
    client = DAVClient(account, account.caldav_url if kind == "calendar" else account.carddav_url)
    count = 0
    for collection in client.collections(kind):
        tree = client.properties(collection, "<d:getetag/><d:resourcetype/>", "1")
        for node in tree.findall(f"{{{DAV}}}response"):
            if node.find(f".//{{{DAV}}}resourcetype/{{{DAV}}}collection") is not None:
                continue
            href = node.findtext(f"{{{DAV}}}href", "")
            etag = node.findtext(f".//{{{DAV}}}getetag", "")
            if not href or not etag:
                continue
            target = client.target(collection, href)
            key = f"dav:{account.id}:{kind}:{target}"
            if store.checkpoint(key) == etag:
                continue
            raw = client.request("GET", target)
            text = raw.decode("utf-8", errors="replace")
            store.put(kind + ":" + account.id, target, {"href": target, "etag": etag,
                      "content": text, "raw_b64": base64.b64encode(raw).decode(),
                      "account_id": account.id}, text)
            store.set_checkpoint(key, etag)
            count += 1
            if count >= limit:
                return count
    return count


def test_account(account: Account) -> dict[str, object]:
    result: dict[str, object] = {}
    if account.mail_enabled:
        with _imap(account) as client:
            status, folders = client.list()
            if status != "OK":
                raise ValueError("Cannot list mailboxes")
            result["imap"] = True
            result["folders"] = [item.decode(errors="replace") for item in folders if isinstance(item, bytes)]
    if account.send_enabled:
        with _smtp(account):
            result["smtp"] = True
    for kind, enabled, url in [("calendar", account.calendar_enabled, account.caldav_url),
                                ("contacts", account.contacts_enabled, account.carddav_url)]:
        if enabled:
            result[kind] = DAVClient(account, url).collections(kind)
    return result


def send_mail(store: PersonalStore, account: Account, operation_id: str,
              recipients: list[str], subject: str, body: str) -> dict[str, object]:
    if not account.enabled or not account.send_enabled:
        raise ValueError("Sending is disabled for this account")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", operation_id):
        raise ValueError("A stable operation ID is required")
    if not recipients or len(recipients) > 20:
        raise ValueError("Provide between 1 and 20 recipients")
    if any("\r" in value or "\n" in value for value in [subject, account.from_address, *recipients]):
        raise ValueError("Header line breaks are not allowed")
    parsed = [address for _, address in getaddresses(recipients)]
    if len(parsed) != len(recipients) or any(not re.fullmatch(r"[^@\s]+@[^@\s]+", address) for address in parsed):
        raise ValueError("Invalid recipient address")
    if len(body) > 1_000_000 or len(subject) > 1000:
        raise ValueError("Message exceeds the sending limit")
    message = EmailMessage()
    message["From"] = account.from_address
    message["To"] = ", ".join(parsed)
    message["Subject"] = subject
    message["Message-ID"] = f"<{operation_id}@{parseaddr(account.from_address)[1].split('@')[-1]}>"
    message.set_content(body)
    digest = hashlib.sha256(json.dumps([account.id, parsed, subject, body]).encode()).hexdigest()
    replayed = store.prepare_delivery(account.id, operation_id, digest, {
        "to": parsed, "subject": subject, "body": body,
        "message_id": str(message["Message-ID"]), "state": "prepared"})
    if replayed:
        return {"id": operation_id, "status": "sent", "replayed": True}
    try:
        with _smtp(account) as smtp:
            refused = smtp.send_message(message)
            if refused:
                raise ValueError("Some recipients were rejected; inspect delivery before retrying")
    except Exception:
        with store.db() as db:
            db.execute("UPDATE operations SET status='uncertain' WHERE id=?", (operation_id,))
        raise
    with store.db() as db:
        db.execute("UPDATE operations SET status='sent' WHERE id=?", (operation_id,))
    store.put("sent:" + account.id, operation_id, {"to": parsed, "subject": subject,
              "body": body, "message_id": str(message["Message-ID"])})
    return {"id": operation_id, "status": "sent", "replayed": False}
