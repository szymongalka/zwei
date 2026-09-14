# WebUI add-ons

This is the Zwei placement and integration policy. The first implemented
feature is [Personal add-ons](./personal-platform.md), with account, inbox, memory
and development tabs. Examples below do not imply other features already exist.

## Decision: tabs for independent tools, panels for context

Add one **Add-ons** (`Dodatki` in Polish) entry to the main navigation when the
first independent add-on is implemented. Inside that view, each available add-on
gets its own tab and uses the main content area. The entry is hidden when no
add-ons are enabled. Do not add an empty Add-ons screen merely to reserve space.

An add-on tab is an application view, not a conversation tab. Existing chat tabs,
pane IDs, workbench layout, and session history keep their existing meaning.

| Need | Placement | Example, when requested |
|---|---|---|
| A task that works independently of the active conversation | Tab inside Add-ons | Notes library, calendar overview, finance dashboard. |
| A small view of the item currently being discussed | Context panel beside the conversation | A selected note, event, document, or diff. |
| Credentials, enable/disable, and preferences | Existing settings/Apps surface or the owning account panel | Connection status and configuration. |
| A short, one-off choice or confirmation | Existing dialog/popover components | Selecting an item or confirming an edit. |
| A separately hosted application with its own lifecycle | Explicit link to that application | An external service that already owns its UI. |

A feature may offer a full tab and a context panel, but they share the same data
model and service calls. Do not create separate implementations or inconsistent
copies of data. On a narrow screen, a context panel becomes a full-width detail
view with a clear way back to the conversation. Independent add-ons remain usable
without creating a fake chat session.

## What exists in the current source

- [`App.tsx`](../webui/src/App.tsx) owns shell view selection and hash routing.
- [`Sidebar.tsx`](../webui/src/components/Sidebar.tsx) owns main navigation.
- [`PaneWorkbench.tsx`](../webui/src/components/workbench/PaneWorkbench.tsx) and
  [`workbench-model.ts`](../webui/src/components/workbench/workbench-model.ts) own
  conversation panes and tabs. Their existence does not provide a generic add-on API.
- [`channel-plugins/registry.ts`](../webui/src/channel-plugins/registry.ts) discovers
  channel UI contributions at build time from `nanobot/channels/*/webui/`. Use it
  for actual channel configuration, not unrelated personal-assistant screens.
- Existing Agent Plugins, CLI Apps, and MCP integrations are managed through Apps;
  their availability does not imply that they can inject arbitrary React views.

These are integration points to inspect again after updating upstream. There is
currently no general dynamic WebUI add-on loader in this checkout.

## Implementation boundary

When the first real independent add-on is requested:

1. Make a small, explicit integration in the shell for the Add-ons view and its
   navigation item. Keep feature logic out of `App.tsx` and `Sidebar.tsx`.
2. Put add-on-specific React components, hooks, API adapters, and state together in
   `webui/src/addons/<id>/`. Create this directory with the implementation, never
   as an empty scaffold. Shared add-on host files belong directly in
   `webui/src/addons/` only when the first implementation needs them.
3. Start with an explicit, typed build-time registration of the implemented add-ons.
   Describe each one's stable ID, translated title, enablement condition, and
   lazy-loaded main view. Add a context-panel entry only for a real panel use case.
   Do not build a marketplace, hot loader, or generalized plugin SDK in advance.
4. Keep add-on selection in a separate hash route, proposed as `#/addons/<id>`.
   Do not store add-on IDs in conversation `paneKeys` or change the workbench's
   persisted version just to display another screen.
5. Reuse existing UI components, design tokens, focus behavior, translations,
   authenticated gateway client, and error handling. Add loading, empty, error,
   and disconnected states; a backend failure must not break chat.
6. Put backend behavior in the existing owning channel, tool, provider, or service
   module. Use MCP or an existing extension mechanism when it fits. If a new
   gateway operation is necessary, define a typed, validated contract at the
   owning edge; do not put UI transport details into the agent loop.
7. Keep credentials on the server. Reuse gateway authentication and existing path
   and network guards. Adding a tab must not grant a wider filesystem or network
   capability. Declare permissions and external writes as part of the feature.
8. Keep the add-on disabled unless deliberately enabled. Configuration fields must
   be declared in the existing schema. Avoid extra servers, login systems, ports,
   and proxy routes for an ordinary in-process add-on.

Settings are for configuring the add-on; daily work belongs in its tab or panel.
Prefer a normal link over an iframe for an external application unless embedding,
authentication, origin policy, and navigation are explicitly designed for it.

## Files and validation

| Artifact | Location |
|---|---|
| Independent add-on source | `webui/src/addons/<id>/`, created with its implementation. |
| Frontend regression tests | Existing `webui/src/tests/` conventions, with the add-on ID in the name. |
| Channel-specific UI | Existing `nanobot/channels/<channel>/webui/`. |
| Backend code and tests | The existing owning Python package and corresponding `tests/` location. |
| User guide and extension notes | `docs/<id>.md` when needed; link from the existing docs index. |
| Build output | Existing `nanobot/web/dist/`, generated by the normal WebUI build. |
| Personal data and runtime state | The documented runtime storage outside the source tree. |

For each implemented add-on, verify:

- enabled and disabled behavior, including navigation and direct-route fallback;
- opening, switching, browser back/forward, reload, and preserved conversation state;
- context-panel behavior and return focus, if a panel exists;
- keyboard operation, small screens, and the existing light/dark themes;
- unauthenticated access, malformed data, disconnected state, and scoped writes;
- the appropriate existing regression tests, feature tests, and `bun run build`.

Record the tested upstream revision and the few shell integration points in the
PR. Use [fork maintenance](./fork-maintenance.md) to verify the same feature after
an upstream update. Create only the directories required by a concrete implementation.
