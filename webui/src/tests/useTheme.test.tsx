import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { useTheme } from "@/hooks/useTheme";

describe("useTheme", () => {
  beforeEach(() => {
    localStorage.removeItem("nanobot-webui.theme");
    localStorage.removeItem("nanobot-webui.theme-style");
    localStorage.removeItem("nanobot-webui.glass-clarity");
    document.documentElement.classList.remove("dark", "theme-liquid-glass");

    const themeColor = document.createElement("meta");
    themeColor.name = "theme-color";
    themeColor.content = "#ffffff";
    themeColor.dataset.themeColorLight = "#ffffff";
    themeColor.dataset.themeColorDark = "#303030";
    themeColor.dataset.themeColorLightGlass = "#eef1f7";
    themeColor.dataset.themeColorDarkGlass = "#080a0f";
    document.head.append(themeColor);
  });

  afterEach(() => {
    document.querySelector('meta[name="theme-color"]')?.remove();
    document.documentElement.classList.remove("dark", "theme-liquid-glass");
    delete document.documentElement.dataset.glassClarity;
    localStorage.removeItem("nanobot-webui.theme");
    localStorage.removeItem("nanobot-webui.theme-style");
    localStorage.removeItem("nanobot-webui.glass-clarity");
  });

  it("keeps browser chrome in sync with the selected app theme", () => {
    localStorage.setItem("nanobot-webui.theme", "dark");
    const { result } = renderHook(useTheme);
    const themeColor = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');

    expect(document.documentElement).toHaveClass("dark");
    expect(themeColor?.content).toBe("#303030");

    act(() => result.current.setTheme("light"));

    expect(document.documentElement).not.toHaveClass("dark");
    expect(themeColor?.content).toBe("#ffffff");
    expect(localStorage.getItem("nanobot-webui.theme")).toBe("light");
  });

  it("defaults to the classic palette with clear glass", () => {
    const { result } = renderHook(useTheme);

    expect(result.current.style).toBe("classic");
    expect(result.current.clarity).toBe("clear");
    expect(document.documentElement).not.toHaveClass("theme-liquid-glass");
    expect(document.documentElement.dataset.glassClarity).toBe("clear");
  });

  it("applies the Liquid Glass palette, its clarity and the glass browser chrome", () => {
    const { result } = renderHook(useTheme);
    const themeColor = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');

    act(() => result.current.setStyle("liquid-glass"));

    expect(document.documentElement).toHaveClass("theme-liquid-glass");
    expect(themeColor?.content).toBe("#eef1f7");

    act(() => result.current.setClarity("tinted"));

    expect(document.documentElement.dataset.glassClarity).toBe("tinted");
    expect(localStorage.getItem("nanobot-webui.glass-clarity")).toBe("tinted");

    act(() => result.current.toggle());

    expect(document.documentElement).toHaveClass("dark");
    expect(themeColor?.content).toBe("#080a0f");
  });

  it("restores the stored palette and clarity on load", () => {
    localStorage.setItem("nanobot-webui.theme-style", "liquid-glass");
    localStorage.setItem("nanobot-webui.glass-clarity", "tinted");

    const { result } = renderHook(useTheme);

    expect(result.current.style).toBe("liquid-glass");
    expect(result.current.clarity).toBe("tinted");
    expect(document.documentElement).toHaveClass("theme-liquid-glass");
    expect(document.documentElement.dataset.glassClarity).toBe("tinted");
  });

  it("clears the glass hooks when the classic palette comes back", () => {
    localStorage.setItem("nanobot-webui.theme-style", "liquid-glass");
    const { result } = renderHook(useTheme);
    const themeColor = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');

    act(() => result.current.setStyle("classic"));

    expect(document.documentElement).not.toHaveClass("theme-liquid-glass");
    expect(themeColor?.content).toBe("#ffffff");
    expect(localStorage.getItem("nanobot-webui.theme-style")).toBe("classic");
  });
});
