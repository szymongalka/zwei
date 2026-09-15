import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { editAccount, emptyAccount, type PersonalStatus, type SavedAccount } from "@/addons/personal/api";
import { PersonalView } from "@/addons/personal/PersonalView";

const mock = vi.hoisted(() => ({ status: vi.fn(), action: vi.fn(), client: {}, getToken: () => "test-token" }));
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => ({ client: mock.client, getToken: mock.getToken }) }));
vi.mock("react-i18next", () => ({ useTranslation: () => ({ i18n: { resolvedLanguage: "en" } }) }));
vi.mock("@/addons/personal/api", async (original) => ({
  ...await original<typeof import("@/addons/personal/api")>(), fetchPersonalStatus: mock.status, personalAction: mock.action,
}));

function status(accounts: SavedAccount[] = []): PersonalStatus {
  return { enabled: true, accounts, remote_configured: true, remote_state: "ready", remote_synced_at: "",
    evolution_enabled: true, retrieval_policy: "hybrid", development_enabled: true, development_state: "pending",
    development_interval_seconds: 86400, memory: { documents: 0, snapshots: 0, pending: 0, compressed_bytes: 0, evolution: [] } };
}
beforeEach(() => {
  vi.clearAllMocks(); window.location.hash = "#/addons/inbox";
  mock.status.mockResolvedValue(status());
  mock.action.mockResolvedValue({ messages: [], total: 0, next_offset: null });
});
afterEach(cleanup);

describe("personal account and inbox boundaries", () => {
  it("keeps organization unconfigured and sending enabled only for agent accounts", () => {
    expect(emptyAccount("agent").send_enabled).toBe(true);
    expect(emptyAccount("mailbox").send_enabled).toBe(false);
    expect(emptyAccount("apple").organize_folders).toBe(false);
    expect(emptyAccount("agent").folder_rules).toEqual([]);
  });

  it("does not send secret-presence metadata back when editing", () => {
    const saved = { ...emptyAccount("agent"), has_password: true, has_smtp_password: true,
      sync_state: "ready", synced_at: "yesterday" };
    const edited = editAccount(saved);
    expect(edited.password).toBe(""); expect(edited.smtp_password).toBe("");
    expect(edited).not.toHaveProperty("has_password");
    expect(edited).not.toHaveProperty("synced_at");
  });

  it("shows initial connection errors even before status exists", async () => {
    mock.status.mockRejectedValue(new Error("offline"));
    render(<PersonalView onBack={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("operation failed");
  });

  it("opens Apple setup with masked passwords and without folder organization controls", async () => {
    render(<PersonalView onBack={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Apple", exact: true }));
    fireEvent.click(screen.getByRole("button", { name: "Add account" }));
    expect(screen.getByLabelText("Password / app password")).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("Read calendars (CalDAV)")).toBeChecked();
    expect(screen.queryByLabelText(/Sort INBOX/)).not.toBeInTheDocument();
    expect(window.location.hash).toBe("#/addons/apple");
  });

  it("renders archived mail as text without activating HTML", async () => {
    mock.action.mockImplementation((_client, payload) => Promise.resolve(payload.action === "get"
      ? { id: "mail", source: "mail:one", content: '<img src="https://tracker.example/pixel" onerror="alert(1)">', next_offset: null, total_chars: 80 }
      : { messages: [{ id: "mail", sender: "sender@example.org", subject: "hello", sent_at: "2026-09-14T08:00:00Z",
          category: "other", priority: 0, preview: "preview", copies: [] }], total: 1, next_offset: null }));
    render(<PersonalView onBack={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Read", exact: true }));
    expect(await screen.findByText(/<img src=/)).toBeInTheDocument();
    expect(document.querySelector('img[src*="tracker.example"]')).toBeNull();
  });

  it("sends a chosen display sort without creating organization rules", async () => {
    render(<PersonalView onBack={vi.fn()} />);
    fireEvent.change(await screen.findByLabelText("Sort"), { target: { value: "oldest" } });
    await waitFor(() => expect(mock.action).toHaveBeenCalledWith(mock.client,
      expect.objectContaining({ action: "inbox", sort: "oldest", account_ids: [] })));
    expect(mock.action.mock.calls.every(([, payload]) => payload.action === "inbox")).toBe(true);
  });
});
