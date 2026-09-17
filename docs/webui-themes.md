# WebUI Appearance Themes

<!-- Meta description: How the Zwei WebUI appearance palettes work — the default classic surfaces and the optional Liquid Glass material, their storage keys, surface hooks, accessibility fallbacks and how to add another palette. -->

The WebUI ships two appearance palettes. `classic` is the default shadcn
surface set; `liquid-glass` is an optional material layered on top. The palette
is orthogonal to light/dark, so there are four combinations: classic light,
classic dark, glass light and glass dark.

| Setting | Values | Storage key |
| --- | --- | --- |
| Theme (light/dark) | `light`, `dark` | `nanobot-webui.theme` |
| Appearance style | `classic` (default), `liquid-glass` | `nanobot-webui.theme-style` |
| Glass clarity | `clear` (default), `tinted` | `nanobot-webui.glass-clarity` |

All three are browser-local: they change rendering, never gateway
configuration, and they apply immediately.

## Where it lives

| File | Role |
| --- | --- |
| `webui/src/hooks/useTheme.ts` | Palette, clarity and light/dark state; `applyTheme()` writes them to `<html>` and to the browser chrome |
| `webui/src/styles/liquid-glass.css` | The whole glass material, namespaced by `.theme-liquid-glass` |
| `webui/src/main.tsx` | Imports the stylesheet *after* `globals.css` |
| `webui/index.html` | Pre-boot script and the extra `theme-color` colours for the glass variants |
| `webui/src/components/settings/overview/OverviewSettings.tsx` | `AppearanceSettings`: the two `SegmentedControl` rows |

State is expressed on the root element only:

```html
<html class="theme-liquid-glass dark" data-glass-clarity="tinted">
```

`AppearanceSettings` renders the clarity row only while the glass palette is
active — clarity means nothing for the classic surfaces.

## Rules for changing the theme

- **Import order is load-bearing.** `liquid-glass.css` must stay imported after
  `globals.css`, and the file must not use `@layer`. The rules have to land
  after the Tailwind utilities, including the `dark:` variants, which share the
  same specificity but are emitted earlier.
- **Colour tokens stay opaque HSL triplets.** The whole app composes tokens as
  `hsl(var(--token) / <alpha>)`; Tailwind injects that alpha itself. Putting
  translucency into `--background` breaks the pattern. Translucency belongs on
  surfaces, not in tokens.
- **Namespace every rule** with `.theme-liquid-glass`, so the classic palette is
  untouched. A new rule without the prefix changes the default look — the
  stylesheet test fails on that.
- **One blur per surface.** A glass surface nested inside another one resets
  `background-color` and `backdrop-filter`, otherwise the backdrop stacks twice
  and the result turns muddy.
- **Opacity grows with the amount of text a layer carries**: canvas < chrome <
  panel < overlay. Menus, dialogs and tooltips are the most opaque.
- **Keep the degradation paths.** `prefers-reduced-transparency`,
  `prefers-contrast: more` and browsers without `backdrop-filter` all collapse
  the material to near-solid `--glass-fill-solid` surfaces.

## Surface hooks

Glass is applied to existing class hooks and ARIA roles, so components do not
need to know about the theme:

```
.bg-background                     canvas veil
.bg-sidebar  .bg-card  .bg-popover .bg-settings-surface
.thread-composer-surface           chrome and floating surfaces
[role="dialog"] [role="alertdialog"] [role="menu"]
[role="listbox"] [role="tooltip"]  overlays (most opaque)
```

Adding a component with its own opaque background means adding its hook to the
stylesheet, not adding a theme prop.

## Adding another palette

1. Add the palette name to `ThemeStyle` in `webui/src/hooks/useTheme.ts`, to
   `readStoredStyle()` and to `applyTheme()` (class on `<html>`).
2. Create `webui/src/styles/<palette>.css` with `.theme-<palette>` scoped rules
   and import it after `globals.css`.
3. Add the option to `AppearanceSettings`, with the label in all locale files
   (`webui/src/i18n/locales/*/common.json`); `webui/src/tests/i18n.test.tsx`
   requires the same key shape in every locale.
4. If the palette needs browser-chrome colours, add `data-theme-color-*-<palette>`
   to the `theme-color` meta tag in `webui/index.html` and extend `browserColorFor()`.
5. Extend `webui/src/tests/theme-<palette>.test.ts` (namespace, tokens, import
   order) and the `useTheme` cases.

## Verification

```bash
cd webui
bun run test      # unit tests, incl. theme-liquid-glass.test.ts
bun run build     # tsc + vite, catches a bad import order or type drift
```

The theme is visual, so also render it: `bun run dev` plus a browser pass over
the login screen, the thread view, the sidebar and Settings → Appearance in all
four combinations. The stylesheet test can prove structure, not looks.

## Sources

The material follows Apple's published guidance rather than a screenshot copy:

- [Human Interface Guidelines — Materials](https://developer.apple.com/design/human-interface-guidelines/materials):
  translucent fills keep content visible through chrome and controls, which is
  what establishes hierarchy between content and controls.
- The iOS 26 cycle increased opacity in navigation chrome and system overlays
  after legibility complaints; every layer here is more opaque the more text it
  carries.
- WWDC 2026 (iOS/iPadOS/macOS 27) reduced the default transparency again and
  added the user-facing control that moves the material between clearer and more
  tinted glass — that control is `data-glass-clarity`.

The web platform has no native refraction, so the material is approximated with
`backdrop-filter` blur plus saturation, a 1px specular top edge and a soft cast
shadow. The transparent brand logo variant is used so the sidebar mark sits on
the material instead of on a plate.