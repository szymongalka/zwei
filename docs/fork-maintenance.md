# Maintaining Zwei

Zwei is the `szymongalka/zwei` fork of
[`HKUDS/nanobot`](https://github.com/HKUDS/nanobot). Its purpose is to add selected capabilities
while keeping the original runtime, interfaces, and release path maintainable.
Compatibility is checked against a specific upstream revision; it is not a promise
that future updates will never require conflict resolution.

## Zwei identity and compatibility

The fork was renamed from `nanobot-plus` to **Zwei**. The canonical repository is
`szymongalka/zwei`. WebUI titles, sign-in, app metadata and icons use Zwei;
`webui/src/lib/branding.ts` contains the small product-copy overlay. Original
translation files remain available for upstream contributions.

The Python package and original CLI remain `nanobot-ai` / `nanobot`. The fork adds
`zwei`: a bare invocation opens the native terminal's session chooser; explicit
arguments continue through the shared nanobot CLI. The optional terminal startup
mode defers chat creation until selection and uses the existing gateway session
list, history and workspace metadata. It introduces no separate session store.
Existing `.nanobot`
data, browser storage keys, API routes, service names and configured source paths
keep their meanings. Renaming a repository does not require moving a working
editable installation. The About view credits the original nanobot project;
its version check describes the inherited runtime, not a separate Zwei release.

The editable icon source is `webui/public/brand/zwei.svg`, an original glass-style
Z monogram. The adjacent 32, 180, 192 and 512 pixel PNG exports support favicon,
Apple touch and web-app installation. Regenerate them from that SVG with a
standards-compliant SVG renderer. The current exports use `@resvg/resvg-js`.
The service worker retains its storage namespace and updates the public icon paths.

## Repository ownership

| Remote | Repository | Purpose |
|---|---|---|
| `origin` | `szymongalka/zwei` | Fork branches, pull requests, and the fork's `main`. |
| `upstream` | `HKUDS/nanobot` | Original source and releases; fetch and compare before integration. |

Verify these URLs in each checkout. If `upstream` is absent, add it with
`git remote add upstream https://github.com/HKUDS/nanobot.git`.
Use `git config remote.pushDefault origin` and `git config pull.ff only` in the
checkout so an ordinary push targets the fork and a pull does not silently create
a merge. A fast-forward pull updates from the configured tracking branch; it does
not reconcile fork changes with upstream by itself.

## Add capabilities at the existing boundaries

1. Prefer configuration, skills, channels, tools, providers, or MCP when the existing
   boundary fits the capability. Keep the agent loop and runner small.
2. For a WebUI addition, follow [WebUI add-ons](./webui-addons.md). An agent plugin or
   channel contribution is not automatically a general-purpose WebUI plugin.
3. Keep custom functionality optional. Disabling it must preserve existing chat,
   settings, session, and automation behavior.
4. When a core change is necessary, isolate the smallest justified change. Avoid
   unrelated renames, formatting, copied upstream modules, or dependency upgrades.
5. Preserve public interfaces and saved data. Describe migrations and rollback when
   a change affects configuration or persistent state.
6. Record the feature's entry points, enabled/disabled behavior, dependencies,
   validation, and upstream revision in the feature documentation and PR.

## Give files a home

Use the existing repository structure: frontend source in `webui/src/`, backend
source in its owning `nanobot/` package, tests next to the established test suite,
and contributor documentation in `docs/`. Channel UI stays with its channel package.

Create a new directory only when a real implementation needs it. Do not add empty
frameworks, placeholder features, `.gitkeep` scaffolds, parallel documentation
trees, or loose scratch files at the repository root. Build output belongs in the
existing generated paths, not in source commits. Runtime workspace, logs, exports,
credentials, and personal profile files belong outside the source repository.

## Develop and review one change

- Fetch the fork and inspect the working tree before starting. Use a topic branch;
  use an isolated worktree when the main checkout is running an editable deployment
  and the task changes executable code.
- Stage explicit files. Inspect the diff for unrelated work, generated artifacts,
  secrets, and accidental personal data before committing or pushing.
- Make focused commits, then push the topic branch to `origin`. A PR should explain
  the problem, the resulting behavior, the upstream interaction, and validation.
- Use the repository's required checks and reviews. Do not bypass them with
  `gh pr merge --admin` or by weakening branch rules.
- For feature PRs, prefer a squash merge when it preserves a useful, focused patch.
  Preserve upstream history with a merge commit for upstream synchronization.
- Before merging, reread the PR head and base, review the final diff and check
  results, and pass the reviewed SHA to `gh pr merge --match-head-commit`.
  If the head changes, repeat review and validation for the new head.
- Use `--auto` only when the repository supports it. An absent check is not a
  passing check: distinguish checks intentionally skipped for documentation from
  failures or missing required validation. Current CI filters documentation-only
  changes; verify those filters when relying on local documentation checks.
- A GitHub merge does not deploy a running editable checkout. Updating source,
  rebuilding frontend assets, and restarting a service are separate operations in
  the scope of the deployment task.

Authorization to work on a repository is supplied by its owner in the active task;
this guide does not grant access to other repositories, account administration,
releases, or deployments.

## Deploy a checked change

When a deployment task updates the running editable checkout, walk this checklist
in order. It exists because a 2026-09-15 restart moved the gateway onto a `main`
that lacked a merged feature required by the live configuration; the rejected
config caused a crash-loop outage. Each step corresponds to one of its recorded
lessons.

1. Record the running revision, then back up the live configuration and built
   frontend assets before switching. Note how each copy is restored.
2. Confirm the working checkout is clean, then switch it to the reviewed commit
   without discarding anyone's uncommitted work. The editable installation stays
   on its existing path.
3. Verify the target revision contains every fork feature the live configuration
   depends on (for example, a `personal` config section requires the personal
   platform in that revision), and that the local checkout and `origin/main` point
   at the same revision. Divergence between them waits for its own failure.
4. Validate the live configuration with the **new** code before any restart —
   including a delayed or scheduled one: run
   `nanobot status --config <path-to-config>` with the updated checkout's
   interpreter and require a clean exit. A positive validation is the release
   gate; a systemd failure after restart is not validation, it is the outage.
5. Restart the service, then check the health endpoint and one authenticated
   operation of each affected add-on. On regression, restore the recorded
   revision, its matching build and the backed-up configuration, then re-check.
   Never roll archive data back to undo a code change.
6. Publish any local merge to the fork's `main` to `origin` immediately after
   merging, so local and remote `main` cannot drift apart between operations.

## Integrate an upstream update

1. Fetch `origin` and `upstream`, verify their default branches, and record the
   exact upstream commit or release tag being integrated.
2. Start an integration branch from the fork's current `origin/main` in an isolated
   checkout. Compare its history with the chosen upstream revision before merging.
3. Merge that revision into the integration branch. Resolve each conflict by
   preserving the intended upstream behavior and the documented custom capability.
   Do not use a blanket `ours`/`theirs` strategy or reset the fork onto upstream.
4. Check whether upstream now implements a custom feature; remove or simplify the
   redundant patch through a reviewed change rather than maintaining both copies.
5. Run the relevant upstream tests and custom feature tests, including disabled
   behavior. For frontend changes, run the WebUI tests and production build. Check
   migration and rollback paths when persistent data changes.
6. Compare any workspace prompt overrides with the new bundled defaults. Overrides
   do not automatically inherit upstream prompt fixes.
7. Open a PR against the fork's `main`, with the upstream revision, resolved
   conflicts, feature compatibility, and actual test results. Merge only after the
   applicable checks and review requirements pass. Keep published history intact.

See [CONTRIBUTING.md](../CONTRIBUTING.md), [design constraints](../.agent/design.md),
and [security boundaries](../.agent/security.md) for the inherited requirements.
