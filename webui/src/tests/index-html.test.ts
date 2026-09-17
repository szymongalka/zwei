import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

describe("index.html", () => {
  it("keeps browser zoom available", () => {
    const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
    const viewport = html.match(/<meta\s+name="viewport"\s+content="([^"]+)"/i)?.[1];

    expect(viewport).toContain("width=device-width");
    expect(viewport).not.toContain("user-scalable=no");
    expect(viewport).not.toMatch(/maximum-scale\s*=\s*1(?:\.0)?(?:,|$)/);
  });

  it("lets iOS keep standalone content inside the safe area", () => {
    const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
    const viewport = html.match(/<meta\s+name="viewport"\s+content="([^"]+)"/i)?.[1];

    expect(viewport).toContain("viewport-fit=auto");
    expect(viewport).not.toContain("viewport-fit=cover");
  });

  it("provides light and dark PWA chrome colors", () => {
    const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
    const manifest = JSON.parse(
      readFileSync(resolve(process.cwd(), "public/manifest.json"), "utf8"),
    ) as {
      background_color?: string;
      theme_color?: string;
      color_scheme_dark?: { background_color?: string; theme_color?: string };
    };
    const document = new DOMParser().parseFromString(html, "text/html");
    const themeColor = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
    const lightBodyBackground = html.match(/body\s*{[^}]*background:\s*([^;]+);/s)?.[1]?.trim();
    const darkBodyBackground = html.match(
      /html\.dark body\s*{[^}]*background:\s*([^;]+);/s,
    )?.[1]?.trim();

    expect(themeColor?.content).toBe("#ffffff");
    expect(themeColor?.dataset.themeColorLight).toBe("#ffffff");
    expect(themeColor?.dataset.themeColorDark).toBe("#303030");
    expect(lightBodyBackground).toBe("#ffffff");
    expect(darkBodyBackground).toBe("#303030");
    expect(manifest.background_color).toBe("#ffffff");
    expect(manifest.theme_color).toBe("#ffffff");
    expect(manifest.color_scheme_dark?.background_color).toBe("#303030");
    expect(manifest.color_scheme_dark?.theme_color).toBe("#303030");
  });

  it("publishes glass chrome colors for the Liquid Glass appearance", () => {
    const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
    const document = new DOMParser().parseFromString(html, "text/html");
    const themeColor = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');

    expect(themeColor?.dataset.themeColorLightGlass).toBe("#eef1f7");
    expect(themeColor?.dataset.themeColorDarkGlass).toBe("#080a0f");
    // The splash colour lives on <html>, not <body>: the body is the glass canvas
    // and must stay translucent for the wallpaper to show through.
    expect(html).toMatch(/html\.theme-liquid-glass\s*{[^}]*background-color:\s*#eef1f7;/s);
    expect(html).toMatch(/html\.theme-liquid-glass\.dark\s*{[^}]*background-color:\s*#080a0f;/s);
    expect(html).not.toMatch(/html\.theme-liquid-glass(?:\.dark)? body\s*{/);
  });

  it("applies the stored appearance style before the bundle loads", () => {
    const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
    const document = new DOMParser().parseFromString(html, "text/html");
    const inline = [...document.querySelectorAll("script")]
      .filter((script) => !script.getAttribute("src"))
      .map((script) => script.textContent ?? "")
      .join("\n");

    expect(inline).toContain("nanobot-webui.theme-style");
    expect(inline).toContain("nanobot-webui.glass-clarity");
    expect(inline).toContain('classList.add("theme-liquid-glass")');
    expect(inline).toContain('setAttribute("data-glass-clarity", clarity)');
    expect(inline).toContain("data-theme-color-dark-glass");
  });
});
