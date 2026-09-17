import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * Contract of the Liquid Glass appearance (docs/webui-themes.md).
 *
 * The theme is optional and namespaced; these checks keep it from leaking into
 * the default look, and keep the invariants the rest of the app depends on
 * (opaque colour tokens, a single blur per surface, real degradation paths).
 */

const read = (relative: string) => readFileSync(resolve(process.cwd(), relative), "utf8");

const css = read("src/styles/liquid-glass.css");
const main = read("src/main.tsx");

/** Comments removed so prose cannot be mistaken for selectors. */
const strippedCss = css.replace(/\/\*[\s\S]*?\*\//g, "");

/**
 * Every rule's selector text. Each match runs from the previous brace (or the
 * previous declaration block) up to the next opening brace, so nested rules
 * inside @media/@supports blocks are covered too.
 */
const ruleSelectors = [...strippedCss.matchAll(/[^{}]*\{/g)]
  .map((match) =>
    match[0]
      .slice(0, -1)
      .split("}")
      .pop()!
      .replace(/\s+/g, " ")
      .trim(),
  )
  .filter((selector) => selector.length > 0);

/** Colour tokens, i.e. everything the app consumes as `hsl(var(--token) / a)`. */
const colourTokens = [...css.matchAll(/^[ \t]*--(?!glass-)([a-z-]+):[ \t]*([^;\n]+);$/gm)];

const clearBlock = css.match(/\.theme-liquid-glass \{([\s\S]*?)\n\}/)?.[1] ?? "";
const tintedBlock =
  css.match(/\.theme-liquid-glass\[data-glass-clarity="tinted"\] \{([\s\S]*?)\n\}/)?.[1] ?? "";

const fillAlpha = (block: string, layer: string) =>
  Number(block.match(new RegExp(`--glass-fill-${layer}: rgb\\([^/]+/ ([\\d.]+)\\)`))?.[1] ?? NaN);

describe("Liquid Glass stylesheet", () => {
  it("loads after globals.css so it wins over the Tailwind utilities", () => {
    expect(main).toContain('import "./globals.css";');
    expect(main).toContain('import "./styles/liquid-glass.css";');
    expect(main.indexOf("./globals.css")).toBeLessThan(main.indexOf("./styles/liquid-glass.css"));
  });

  it("keeps every rule inside the opt-in namespace", () => {
    expect(css).not.toContain("@layer");
    expect(ruleSelectors.length).toBeGreaterThan(15);

    for (const selector of ruleSelectors) {
      const scoped = selector.startsWith(".theme-liquid-glass");
      expect(selector.startsWith("@") || scoped, `unscoped selector: ${selector}`).toBe(true);
    }
  });

  it("ships a light and a dark variant of the same material", () => {
    expect(ruleSelectors).toContain(".theme-liquid-glass");
    expect(ruleSelectors).toContain(".theme-liquid-glass.dark");
  });

  it("keeps colour tokens opaque HSL triplets", () => {
    expect(colourTokens.length).toBeGreaterThan(20);

    for (const [, token, value] of colourTokens) {
      expect(value.trim(), `--${token}`).toMatch(/^\d+(?:\.\d+)? \d+(?:\.\d+)?% \d+(?:\.\d+)?%$/);
    }
  });

  it("applies translucency per surface instead of in the tokens", () => {
    const surfaceRule = ruleSelectors.find(
      (selector) => selector.includes(":is(") && selector.includes(".bg-sidebar"),
    );

    expect(surfaceRule).toBeDefined();
    for (const hook of [
      ".bg-background",
      ".bg-sidebar",
      ".bg-card",
      ".bg-popover",
      ".bg-settings-surface",
      ".thread-composer-surface",
      '[role="dialog"]',
      '[role="alertdialog"]',
      '[role="menu"]',
      '[role="listbox"]',
      '[role="tooltip"]',
    ]) {
      expect(css, `missing surface hook ${hook}`).toContain(hook);
    }
  });

  it("blurs the material behind surfaces and gives it a specular edge", () => {
    expect(css).toContain("backdrop-filter: saturate(var(--glass-saturate)) blur(var(--glass-blur));");
    expect(css).toContain("-webkit-backdrop-filter: saturate(var(--glass-saturate)) blur(var(--glass-blur));");
    expect(css).toContain("inset 0 1px 0 var(--glass-specular)");
  });

  it("increases opacity with the amount of text a layer carries", () => {
    const canvas = fillAlpha(clearBlock, "canvas");
    const chrome = fillAlpha(clearBlock, "chrome");
    const panel = fillAlpha(clearBlock, "panel");
    const overlay = fillAlpha(clearBlock, "overlay");

    expect([canvas, chrome, panel, overlay].every(Number.isFinite)).toBe(true);
    expect(canvas).toBeLessThan(chrome);
    expect(chrome).toBeLessThan(panel);
    expect(panel).toBeLessThan(overlay);
    expect(overlay).toBeLessThan(1);
  });

  it("moves the material towards tinted glass with the clarity control", () => {
    expect(ruleSelectors).toContain('.theme-liquid-glass[data-glass-clarity="tinted"]');
    expect(ruleSelectors).toContain('.theme-liquid-glass.dark[data-glass-clarity="tinted"]');

    expect(fillAlpha(tintedBlock, "canvas")).toBeGreaterThan(fillAlpha(clearBlock, "canvas"));
    expect(fillAlpha(tintedBlock, "overlay")).toBeGreaterThan(fillAlpha(clearBlock, "overlay"));
  });

  it("degrades to readable, solid surfaces", () => {
    for (const marker of [
      "@media (prefers-reduced-transparency: reduce)",
      "@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px)))",
      "@media (prefers-contrast: more)",
    ]) {
      expect(css, `missing fallback ${marker}`).toContain(marker);
    }

    // Every fallback flattens the blur instead of leaving a half-transparent surface.
    expect(css.match(/--glass-blur: 0px;/g)?.length).toBeGreaterThanOrEqual(3);
    expect(css.match(/--glass-fill-panel: var\(--glass-fill-solid\);/g)?.length).toBeGreaterThanOrEqual(3);
    expect(css).toContain("background-image: none;");
  });

  it("never stacks a second blur inside an already blurred surface", () => {
    const nested = ruleSelectors.find((selector) => selector.includes(":is(.glass-surface, .bg-sidebar, [role=\"dialog\"])"));

    expect(nested).toBeDefined();
    expect(css).toContain("backdrop-filter: none;");
  });
});