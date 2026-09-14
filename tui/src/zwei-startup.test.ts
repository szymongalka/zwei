import { afterEach, expect, test } from "bun:test"
import { TextareaRenderable } from "@opentui/core"
import { createTestRenderer, MockTreeSitterClient, type TestRendererSetup } from "@opentui/core/testing"
import { NanobotTui, type AppOptions } from "./app"
import type { RecoveryState, WorkspaceScopePayload } from "./protocol"
import type { SessionMenu } from "./session-menu"

let setup: TestRendererSetup | undefined
let app: NanobotTui | undefined
const originalFetch = globalThis.fetch

afterEach(() => {
  app?.stop()
  app = undefined
  setup = undefined
  globalThis.fetch = originalFetch
})

async function waitUntil(predicate: () => boolean): Promise<void> {
  for (let i = 0; i < 200 && !predicate(); i++) await Bun.sleep(5)
  expect(predicate()).toBe(true)
}

async function fixture(empty = false) {
  const requests: string[] = []
  let failSessions = false
  globalThis.fetch = ((input: string | URL | Request) => {
    const url = String(input)
    requests.push(url)
    if (url.endsWith("/api/sessions")) {
      if (failSessions) return Promise.resolve(new Response("unavailable", { status: 503 }))
      return Promise.resolve(Response.json({ sessions: empty ? [] : [{
        key: "websocket:existing", title: "Saved work", preview: "Previous conversation",
        model_preset: "Astra", workspace_scope: { project_path: "/saved-project", access_mode: "restricted" },
      }] }))
    }
    if (url.includes("/webui-thread?")) return Promise.resolve(Response.json({
      messages: [{ role: "user", content: "Remember this history" }], page: { has_more_before: false },
    }))
    return Promise.resolve(Response.json({}))
  }) as typeof fetch
  const attached: string[] = []
  const created: Array<WorkspaceScopePayload | undefined> = []
  const sent: string[] = []
  const transport = {
    activeChatId: "",
    connect() {}, close() {},
    send(content: string) { sent.push(content); return "turn" },
    attach(chatId: string) { attached.push(chatId); this.activeChatId = chatId },
    newChat(scope?: WorkspaceScopePayload) { created.push(scope); this.activeChatId = "fresh" },
    setWorkspaceScope(_scope: WorkspaceScopePayload) {},
    async updateRecovery(): Promise<RecoveryState> { return { status: "recovered", recovery_id: "fixture" } },
  }
  const options: AppOptions = {
    startWithSessions: true, apiUrl: "http://fixture.invalid", apiToken: "fixture",
    model: "test/Sol", modelPreset: "default", workspace: "/personal",
    version: "test", access: "workspace access", theme: "dark",
  }
  setup = await createTestRenderer({ width: 90, height: 24, screenMode: "alternate-screen" })
  app = NanobotTui.mount(setup.renderer, options, transport, new MockTreeSitterClient({ autoResolveTimeout: 0 }))
  const ui = app as unknown as {
    sessionMenu: SessionMenu; composer: TextareaRenderable; ready: boolean
    status: { plainText: string }; modelPreset: string
    runtimeControls: { workspaceScope: WorkspaceScopePayload }
  }
  app.accept({ event: "ready", chat_id: "", client_id: "fixture" })
  await waitUntil(() => ui.sessionMenu.visible)
  return { ui, requests, attached, created, sent, setFail: (value: boolean) => { failSessions = value } }
}

test("startup chooser cancels without creating or resuming a conversation", async () => {
  const { ui, attached, created, sent } = await fixture()
  await setup!.renderOnce()
  expect(setup!.captureCharFrame()).toContain("Nowa sesja")
  expect(setup!.captureCharFrame()).toContain("Saved work")
  expect(ui.sessionMenu.newChatSelected).toBe(true)
  setup!.mockInput.pressEscape()
  await waitUntil(() => setup!.renderer.isDestroyed)
  expect(attached).toEqual([])
  expect(created).toEqual([])
  expect(sent).toEqual([])
})

test("first selected session restores history, model and workspace without sending a prompt", async () => {
  const { ui, requests, attached, created, sent } = await fixture()
  ui.composer.setText("saved")
  ui.composer.submit()
  await waitUntil(() => attached.length === 1)
  app!.accept({ event: "attached", chat_id: "existing" })
  await waitUntil(() => ui.ready)
  await setup!.renderOnce()
  expect(attached).toEqual(["existing"])
  expect(created).toEqual([])
  expect(sent).toEqual([])
  expect(requests.some((url) => url.includes("websocket%3Aexisting/webui-thread?"))).toBe(true)
  expect(setup!.captureCharFrame()).toContain("Remember this history")
  expect(ui.modelPreset).toBe("Astra")
  expect(ui.runtimeControls.workspaceScope.project_path).toBe("/saved-project")
})

test("empty account offers new session in the configured workspace", async () => {
  const { ui, attached, created, sent } = await fixture(true)
  expect(ui.sessionMenu.newChatSelected).toBe(true)
  ui.composer.submit()
  await waitUntil(() => created.length === 1)
  expect(created).toEqual([{ project_path: "/personal", access_mode: "restricted" }])
  expect(attached).toEqual([])
  expect(sent).toEqual([])
})

test("unmatched search does not create a session and clearing it restores the new action", async () => {
  const { ui, created, attached } = await fixture()
  ui.composer.setText("no such conversation")
  ui.composer.submit()
  await Bun.sleep(50)
  expect(ui.sessionMenu.choose()).toBeNull()
  expect(ui.sessionMenu.newChatSelected).toBe(false)
  expect(created).toEqual([])
  expect(attached).toEqual([])
  ui.composer.setText("")
  await waitUntil(() => ui.sessionMenu.newChatSelected)
})

test("session list failure can be retried after reconnect without sending a prompt", async () => {
  const { ui, setFail, sent, created } = await fixture()
  setFail(true)
  ui.sessionMenu.hide()
  app!.accept({ event: "ready", chat_id: "", client_id: "fixture" })
  await waitUntil(() => ui.status.plainText.includes("Enter ponów"))
  setFail(false)
  ui.composer.submit()
  await waitUntil(() => ui.sessionMenu.visible)
  expect(created).toEqual([])
  expect(sent).toEqual([])
})
