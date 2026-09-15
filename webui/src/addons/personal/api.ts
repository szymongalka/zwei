import { fetchWithTimeout } from "@/lib/http";
import type { NanobotClient } from "@/lib/nanobot-client";

export type AccountKind = "apple" | "agent" | "mailbox";
export interface AccountInput {
  id: string; label: string; kind: AccountKind; username: string; password: string;
  smtp_username: string; smtp_password: string; enabled: boolean; include_inbox: boolean;
  organize_folders: boolean; mail_enabled: boolean; calendar_enabled: boolean; contacts_enabled: boolean;
  folder_rules: { id: string; label: string; folder: string; contains: string[]; senders: string[] }[];
  send_enabled: boolean; imap_host: string; imap_port: number; smtp_host: string;
  smtp_port: number; smtp_security: "starttls" | "tls"; folders: string[];
  caldav_url: string; carddav_url: string; from_address: string;
  max_message_bytes: number;
}
export interface SavedAccount extends Omit<AccountInput, "password" | "smtp_password"> {
  has_password: boolean; has_smtp_password: boolean; sync_state: string; synced_at: string;
}
export interface PersonalStatus {
  enabled: boolean; accounts: SavedAccount[]; remote_configured: boolean;
  remote_state: string; remote_synced_at: string; evolution_enabled: boolean;
  retrieval_policy: string;
  development_enabled: boolean; development_state: string; development_interval_seconds: number;
  memory: { documents: number; snapshots: number; pending: number; compressed_bytes: number;
    evolution: { id: number; created: string; status: string; detail: Record<string, unknown> }[] };
}
export interface MemoryRecord { id: string; source: string; excerpt: string; created: string }
export interface RecordPage { id: string; source: string; content: string; next_offset: number | null; total_chars: number }
export interface InboxMessage {
  id: string; sender: string; subject: string; sent_at: string; category: string;
  priority: number; preview: string; copies: { id: string; account_id: string }[];
}
export interface InboxPage { messages: InboxMessage[]; total: number; next_offset: number | null }

export async function fetchPersonalStatus(token: string): Promise<PersonalStatus | null> {
  const response = await fetchWithTimeout("/api/personal/status", { headers: { Authorization: `Bearer ${token}` } });
  if (response.status === 404) return null;
  if (!response.ok) throw new Error("Personal status unavailable");
  const data = await response.json() as { enabled?: boolean; result?: PersonalStatus };
  return data.result?.enabled ? data.result : null;
}

export function personalAction<T>(client: NanobotClient, payload: Record<string, unknown>): Promise<T> {
  return client.requestMutation<{ result: T }>("personal.action", payload, 180_000).then(data => data.result);
}

export function emptyAccount(kind: AccountKind): AccountInput {
  return { id: crypto.randomUUID(), kind, label: "", username: "", password: "", smtp_username: "", smtp_password: "",
    enabled: true, include_inbox: true, organize_folders: false, folder_rules: [], mail_enabled: true,
    calendar_enabled: kind === "apple", contacts_enabled: kind === "apple", send_enabled: kind === "agent",
    imap_host: kind === "apple" ? "imap.mail.me.com" : "", imap_port: 993,
    smtp_host: kind === "apple" ? "smtp.mail.me.com" : "", smtp_port: 587, smtp_security: "starttls", folders: ["INBOX"],
    caldav_url: "https://caldav.icloud.com/", carddav_url: "https://contacts.icloud.com/", from_address: "", max_message_bytes: 25_000_000 };
}

export function editAccount(account: SavedAccount): AccountInput {
  const result = emptyAccount(account.kind);
  for (const key of Object.keys(result) as (keyof AccountInput)[]) {
    if (key !== "password" && key !== "smtp_password" && key in account) {
      Object.assign(result, { [key]: account[key] });
    }
  }
  return result;
}
