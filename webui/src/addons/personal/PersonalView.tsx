import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useClient } from "@/providers/ClientProvider";

import { editAccount, emptyAccount, fetchPersonalStatus, personalAction,
  type AccountInput, type InboxPage, type MemoryRecord,
  type PersonalStatus, type RecordPage } from "./api";
import { messages } from "./messages";

type Tab = "inbox" | "apple" | "agent" | "mailbox" | "memory" | "evolution";
const tabs: Tab[] = ["inbox", "apple", "agent", "mailbox", "memory", "evolution"];
type Copy = typeof messages.en;
function readTab(): Tab {
  const value = window.location.hash.split("?")[0].split("/")[2];
  return tabs.includes(value as Tab) ? value as Tab : "inbox";
}

export function PersonalView({ onBack }: { onBack: () => void }) {
  const { i18n } = useTranslation();
  const c = i18n.resolvedLanguage?.startsWith("pl") ? messages.pl : messages.en;
  const { client, getToken } = useClient();
  const [tab, setTab] = useState<Tab>(readTab);
  const [status, setStatus] = useState<PersonalStatus | null | undefined>();
  const [form, setForm] = useState<AccountInput | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<MemoryRecord[]>([]);
  const [inbox, setInbox] = useState<InboxPage>({ messages: [], total: 0, next_offset: null });
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string[]>([]);
  const [sort, setSort] = useState("newest");
  const [page, setPage] = useState<RecordPage | null>(null);
  const [compose, setCompose] = useState(false);
  const refresh = useCallback(async () => setStatus(await fetchPersonalStatus(getToken())), [getToken]);

  useEffect(() => {
    let active = true;
    const load = () => { void fetchPersonalStatus(getToken()).then(s => { if (active) setStatus(s); }).catch(() => { if (active) setError(c.failed); }); };
    load();
    const timer = window.setInterval(load, 30_000);
    const onHash = () => { setTab(readTab()); setForm(null); setPage(null); };
    window.addEventListener("hashchange", onHash);
    return () => { active = false; window.clearInterval(timer); window.removeEventListener("hashchange", onHash); };
  }, [getToken, c.failed]);

  useEffect(() => {
    if (tab !== "inbox" || !status?.enabled) return;
    let active = true;
    void personalAction<InboxPage>(client, { action: "inbox", account_ids: selected, offset, limit: 20, sort })
      .then(value => { if (active) setInbox(value); }).catch(() => { if (active) setError(c.failed); });
    return () => { active = false; };
  }, [client, tab, offset, selected, sort, status, c.failed]);

  async function run(operation: () => Promise<void>) {
    setBusy(true); setError(""); setNotice("");
    try { await operation(); setNotice(c.done); await refresh(); }
    catch { setError(c.failed); }
    finally { setBusy(false); }
  }
  function switchTab(next: Tab) {
    const suffix = window.location.hash.split("?")[1];
    window.location.hash = `#/addons/${next}${suffix ? `?${suffix}` : ""}`;
    setTab(next); setForm(null); setPage(null); setNotice(""); setError("");
  }
  const accounts = status?.accounts.filter(account => account.kind === tab) ?? [];

  return <section className="h-full overflow-y-auto bg-background p-4 sm:p-8" aria-label={c.title}>
    <div className="mx-auto max-w-6xl space-y-5">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">{c.title}</h1>
        <div className="flex gap-2"><Button variant="outline" disabled={busy} onClick={() => void run(refresh)}>{c.refresh}</Button><Button variant="ghost" onClick={onBack}>{c.back}</Button></div>
      </header>
      {status === undefined && <p role="status">{c.loading}</p>}
      {status === null && <p>{c.disabled}</p>}
      {error && <p role="alert" className="rounded-lg border border-destructive p-3 text-destructive">{error}</p>}
      {status && <>
        <nav className="flex flex-wrap gap-2" aria-label={c.title}>{tabs.map(value =>
          <Button key={value} variant={tab === value ? "default" : "outline"} aria-current={tab === value ? "page" : undefined} onClick={() => switchTab(value)}>{c[value]}</Button>)}</nav>
        {(busy || notice) && <p role="status" className="text-sm text-muted-foreground">{busy ? c.working : notice}</p>}
        {(tab === "apple" || tab === "agent" || tab === "mailbox") && <>
          {!form && <Button onClick={() => setForm(emptyAccount(tab))}>{c.add}</Button>}
          {form && <AccountForm value={form} copy={c} busy={busy} onChange={setForm} onCancel={() => setForm(null)} onSave={() => void run(async () => {
            await personalAction(client, { action: "save_account", account: form }); setForm(null);
          })} />}
          {!accounts.length && !form && <p className="text-muted-foreground">{c.noAccounts}</p>}
          <div className="grid gap-4 md:grid-cols-2">{accounts.map(account => <article key={account.id} className="space-y-3 rounded-xl border p-4">
            <h2 className="font-semibold">{account.label}</h2><p className="break-all text-sm">{account.username}</p>
            <p className="text-sm text-muted-foreground">{account.sync_state} {account.synced_at && new Date(account.synced_at).toLocaleString(i18n.resolvedLanguage)}</p>
            <div className="flex flex-wrap gap-2"><Button variant="outline" disabled={busy} onClick={() => setForm(editAccount(account))}>{c.edit}</Button>
              <Button variant="outline" disabled={busy} onClick={() => void run(async () => { await personalAction(client, { action: "test_account", account_id: account.id }); })}>{c.test}</Button>
              <Button disabled={busy || !account.enabled} onClick={() => void run(async () => { await personalAction(client, { action: "sync_account", account_id: account.id }); })}>{c.sync}</Button></div>
          </article>)}</div>
        </>}
        {tab === "inbox" && <>
          <div className="flex flex-wrap items-center gap-3">
            <label className="text-sm">{c.selected}<select className="ml-2 rounded border bg-background p-2" value={selected[0] ?? ""} onChange={event => { setSelected(event.target.value ? [event.target.value] : []); setOffset(0); }}>
              <option value="">{c.all}</option>{status.accounts.filter(a => a.include_inbox).map(a => <option key={a.id} value={a.id}>{a.label}</option>)}</select></label>
            <span className="text-sm text-muted-foreground">{c.count}: {inbox.total}</span>
            <label className="text-sm">{c.sort}<select className="ml-2 rounded border bg-background p-2" value={sort} onChange={event => { setSort(event.target.value); setOffset(0); }}>
              <option value="newest">{c.newest}</option><option value="oldest">{c.oldest}</option><option value="sender">{c.sender}</option></select></label>
            <Button disabled={!status.accounts.some(a => a.enabled && a.send_enabled)} onClick={() => setCompose(!compose)}>{c.compose}</Button>
          </div>
          {compose && <Compose status={status} copy={c} busy={busy} onCancel={() => setCompose(false)} onSend={(payload) => run(async () => { await personalAction(client, { action: "send_mail", ...payload }); setCompose(false); })} />}
          {!inbox.messages.length && <p className="py-8 text-muted-foreground">{c.noMessages}</p>}
          <div className="divide-y rounded-xl border">{inbox.messages.map(message => <article key={message.id} className="space-y-1 p-4">
            <div className="flex flex-wrap justify-between gap-2"><h2 className="font-medium">{message.subject || c.emptySubject}</h2><time className="text-xs text-muted-foreground">{new Date(message.sent_at).toLocaleString(i18n.resolvedLanguage)}</time></div>
            <p className="break-all text-sm">{message.sender}</p><p className="text-sm text-muted-foreground">{message.preview}</p>
            <div className="flex flex-wrap items-center gap-3"><span className="text-xs text-muted-foreground">{message.copies.map(item => status.accounts.find(a => a.id === item.account_id)?.label ?? item.account_id).join(" · ")}</span>
              <Button variant="ghost" size="sm" disabled={busy} onClick={() => void run(async () => setPage(await personalAction(client, { action: "get", document_id: message.id })))}>{c.read}</Button></div>
          </article>)}</div>
          <div className="flex gap-2"><Button variant="outline" disabled={offset === 0 || busy} onClick={() => setOffset(Math.max(0, offset - 20))}>{c.previous}</Button>
            <Button variant="outline" disabled={inbox.next_offset === null || busy} onClick={() => setOffset(inbox.next_offset ?? offset)}>{c.next}</Button></div>
        </>}
        {tab === "memory" && <>
          <p className="text-sm text-muted-foreground">{c.archiveHelp}</p>
          <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">{([[c.documents, status.memory.documents], [c.snapshots, status.memory.snapshots], [c.pending, status.memory.pending], [c.remote, status.remote_state]] as const).map(([label, value]) => <div className="rounded-xl border p-4" key={label}><dt className="text-sm text-muted-foreground">{label}</dt><dd className="mt-2 break-all text-lg font-semibold">{value}</dd></div>)}</dl>
          <Button disabled={busy || !status.remote_configured} onClick={() => void run(async () => { await personalAction(client, { action: "sync_memory" }); })}>{c.sync}</Button>
          <form className="flex gap-2" onSubmit={event => { event.preventDefault(); void run(async () => setResults(await personalAction(client, { action: "search", query }))); }}>
            <Input aria-label={c.query} placeholder={c.query} value={query} onChange={event => setQuery(event.target.value)} /><Button disabled={busy || !query.trim()}>{c.search}</Button></form>
          <div className="space-y-3">{results.map(result => <article key={result.id} className="rounded-xl border p-4"><p className="text-xs text-muted-foreground">{result.source}</p><p className="my-2 whitespace-pre-wrap text-sm">{result.excerpt}</p>
            <Button variant="outline" disabled={busy} onClick={() => void run(async () => setPage(await personalAction(client, { action: "get", document_id: result.id })))}>{c.read}</Button></article>)}</div>
        </>}
        {tab === "evolution" && <>
          <h2 className="font-semibold">{c.development}</h2><p className="text-sm text-muted-foreground">{c.developmentHelp}</p>
          <p>{status.development_enabled ? status.development_state : c.disabled}</p>
          <h2 className="font-semibold">{c.autoEvolution}</h2><p className="max-w-3xl text-sm text-muted-foreground">{c.evolutionHelp}</p>
          <p>{c.policy}: <strong>{status.retrieval_policy}</strong></p><div className="flex gap-2"><Button disabled={busy} onClick={() => void run(async () => { await personalAction(client, { action: "evolve" }); })}>{c.experiment}</Button>
            <Button variant="outline" disabled={busy || !status.memory.evolution.some(e => e.status === "promoted")} onClick={() => void run(async () => { await personalAction(client, { action: "rollback" }); })}>{c.rollback}</Button></div>
          {!status.memory.evolution.length && <p>{c.noExperiments}</p>}
          {status.memory.evolution.map(item => <article className="rounded-xl border p-4" key={item.id}><p>{new Date(item.created).toLocaleString(i18n.resolvedLanguage)} · {item.status}</p><details className="mt-2"><summary>{c.read}</summary><pre className="overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(item.detail, null, 2)}</pre></details></article>)}
        </>}
        {page && <aside className="space-y-3 rounded-xl border bg-muted/30 p-4" aria-label={c.read}>
          <div className="flex justify-between gap-2"><p className="break-all text-xs">{page.source}</p><Button variant="ghost" onClick={() => setPage(null)}>{c.close}</Button></div>
          <pre className="whitespace-pre-wrap break-words font-sans text-sm">{page.content}</pre>
          {page.next_offset !== null && <Button disabled={busy} onClick={() => void run(async () => setPage(await personalAction(client, { action: "get", document_id: page.id, offset: page.next_offset })))}>{c.more}</Button>}
        </aside>}
      </>}
    </div>
  </section>;
}

function AccountForm({ value, copy: c, busy, onChange, onCancel, onSave }: {
  value: AccountInput; copy: Copy; busy: boolean; onChange: (value: AccountInput) => void; onCancel: () => void; onSave: () => void;
}) {
  function text(key: keyof AccountInput, label: string, type = "text", required = false) {
    return <label className="block space-y-1 text-sm" key={key}><span>{label}</span><Input required={required} type={type} value={String(value[key])} autoComplete={type === "password" ? "new-password" : "off"}
      onChange={event => onChange({ ...value, [key]: type === "number" ? Number(event.target.value) : event.target.value })} /></label>;
  }
  function check(key: "enabled" | "include_inbox" | "mail_enabled" | "calendar_enabled" | "contacts_enabled" | "send_enabled", label: string) {
    return <label className="flex items-center gap-2 text-sm" key={key}><input type="checkbox" checked={value[key]} onChange={event => onChange({ ...value, [key]: event.target.checked })} />{label}</label>;
  }
  return <form className="space-y-4 rounded-xl border p-4" onSubmit={event => { event.preventDefault(); onSave(); }}>
    {value.kind === "apple" && <p className="text-sm">{c.appleHelp} <a className="underline" href="https://account.apple.com" target="_blank" rel="noreferrer">Apple Account</a></p>}
    <div className="grid gap-3 sm:grid-cols-2">{text("label", c.name, "text", true)}{text("username", c.login, "text", true)}{text("password", c.password, "password")}{value.mail_enabled && text("imap_host", "IMAP host", "text", true)}</div>
    <p className="text-xs text-muted-foreground">{c.passwordHelp}</p>
    <div className="flex flex-wrap gap-4">{check("enabled", c.enabled)}{check("include_inbox", c.unified)}{check("mail_enabled", c.mail)}{check("send_enabled", c.sending)}
      {value.kind === "apple" && <>{check("calendar_enabled", c.calendar)}{check("contacts_enabled", c.contacts)}</>}</div>
    {value.send_enabled && <div className="grid gap-3 sm:grid-cols-2">{text("from_address", c.from, "email")}{text("smtp_host", "SMTP host", "text", true)}{text("smtp_username", c.smtpLogin)}{text("smtp_password", c.smtpPassword, "password")}</div>}
    <details><summary className="cursor-pointer text-sm">{c.advanced}</summary><div className="mt-3 grid gap-3 sm:grid-cols-2">
      {text("imap_port", "IMAP port", "number")}{text("smtp_port", "SMTP port", "number")}
      <label className="space-y-1 text-sm">SMTP TLS<select className="block w-full rounded border bg-background p-2" value={value.smtp_security} onChange={event => onChange({ ...value, smtp_security: event.target.value as "starttls" | "tls" })}><option value="starttls">STARTTLS</option><option value="tls">TLS</option></select></label>
      {value.kind === "apple" && <>{text("caldav_url", "CalDAV URL")}{text("carddav_url", "CardDAV URL")}</>}
      <label className="space-y-1 text-sm sm:col-span-2">{c.folders}<Textarea value={value.folders.join("\n")} onChange={event => onChange({ ...value, folders: event.target.value.split("\n") })} /></label>
    </div></details>
    <div className="flex gap-2"><Button disabled={busy}>{c.save}</Button><Button type="button" variant="outline" onClick={onCancel}>{c.cancel}</Button></div>
  </form>;
}

function Compose({ status, copy: c, busy, onCancel, onSend }: { status: PersonalStatus; copy: Copy; busy: boolean; onCancel: () => void; onSend: (payload: Record<string, unknown>) => Promise<void> }) {
  const senders = status.accounts.filter(a => a.enabled && a.send_enabled);
  const [account, setAccount] = useState(senders[0]?.id ?? "");
  const [to, setTo] = useState(""); const [subject, setSubject] = useState(""); const [body, setBody] = useState("");
  // Stable for this draft, including uncertain reconnects and repeated button presses.
  const [operationId] = useState(() => crypto.randomUUID());
  return <form className="space-y-3 rounded-xl border p-4" onSubmit={event => { event.preventDefault(); void onSend({ account_id: account, operation_id: operationId, recipients: to.split(",").map(v => v.trim()).filter(Boolean), subject, body }); }}>
    <label className="block text-sm">{c.from}<select className="ml-2 rounded border bg-background p-2" value={account} onChange={event => setAccount(event.target.value)}>{senders.map(a => <option key={a.id} value={a.id}>{a.label} — {a.from_address}</option>)}</select></label>
    <Input required aria-label={c.recipients} placeholder={c.recipients} value={to} onChange={event => setTo(event.target.value)} />
    <Input required aria-label={c.subject} placeholder={c.subject} value={subject} onChange={event => setSubject(event.target.value)} />
    <Textarea required aria-label={c.body} placeholder={c.body} value={body} onChange={event => setBody(event.target.value)} />
    <div className="flex gap-2"><Button disabled={busy || !account}>{c.send}</Button><Button type="button" variant="outline" onClick={onCancel}>{c.cancel}</Button></div>
  </form>;
}
