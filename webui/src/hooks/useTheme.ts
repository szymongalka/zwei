import {
  createContext,
  createElement,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";

/** Colour mode. */
export type Theme = "light" | "dark";
/** Appearance palette: the default shadcn surfaces or the Liquid Glass material. */
export type ThemeStyle = "classic" | "liquid-glass";
/** Liquid Glass clarity: Apple's "clearer vs more tinted" control (WWDC 2026). */
export type GlassClarity = "clear" | "tinted";

const STORAGE_KEY = "nanobot-webui.theme";
export const THEME_STYLE_STORAGE_KEY = "nanobot-webui.theme-style";
export const GLASS_CLARITY_STORAGE_KEY = "nanobot-webui.glass-clarity";

/** Class the Liquid Glass stylesheet is namespaced by. */
export const LIQUID_GLASS_CLASS = "theme-liquid-glass";

const ThemeContext = createContext<Theme>("light");

function readStoredTheme(): Theme | null {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return null;
  }
}

function readStoredStyle(): ThemeStyle {
  try {
    return localStorage.getItem(THEME_STYLE_STORAGE_KEY) === "liquid-glass"
      ? "liquid-glass"
      : "classic";
  } catch {
    return "classic";
  }
}

function readStoredClarity(): GlassClarity {
  try {
    return localStorage.getItem(GLASS_CLARITY_STORAGE_KEY) === "tinted" ? "tinted" : "clear";
  } catch {
    return "clear";
  }
}

function browserColorFor(
  themeColor: HTMLMetaElement | null,
  theme: Theme,
  style: ThemeStyle,
): string | undefined {
  if (!themeColor) return undefined;
  const glass = style === "liquid-glass";
  const light = glass ? themeColor.dataset.themeColorLightGlass : undefined;
  const dark = glass ? themeColor.dataset.themeColorDarkGlass : undefined;
  const color =
    theme === "dark"
      ? dark ?? themeColor.dataset.themeColorDark
      : light ?? themeColor.dataset.themeColorLight;
  return color;
}

/** Applies mode, palette and clarity to <html> and to the browser chrome. */
export function applyTheme(theme: Theme, style: ThemeStyle, clarity: GlassClarity): void {
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  root.classList.toggle(LIQUID_GLASS_CLASS, style === "liquid-glass");
  root.dataset.glassClarity = clarity;

  const themeColor = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  const color = browserColorFor(themeColor, theme, style);
  if (themeColor && color) themeColor.content = color;
}

export function useTheme(): {
  theme: Theme;
  style: ThemeStyle;
  clarity: GlassClarity;
  toggle: () => void;
  setTheme: (t: Theme) => void;
  setStyle: (s: ThemeStyle) => void;
  setClarity: (c: GlassClarity) => void;
} {
  const [theme, setThemeState] = useState<Theme>(() => {
    const stored = readStoredTheme();
    if (stored) return stored;
    if (typeof window !== "undefined" && window.matchMedia) {
      return window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light";
    }
    return "light";
  });
  const [style, setStyleState] = useState<ThemeStyle>(readStoredStyle);
  const [clarity, setClarityState] = useState<GlassClarity>(readStoredClarity);

  useEffect(() => {
    applyTheme(theme, style, clarity);
  }, [theme, style, clarity]);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, theme);
      localStorage.setItem(THEME_STYLE_STORAGE_KEY, style);
      localStorage.setItem(GLASS_CLARITY_STORAGE_KEY, clarity);
    } catch {
      // ignore
    }
  }, [theme, style, clarity]);

  const setTheme = useCallback((t: Theme) => setThemeState(t), []);
  const setStyle = useCallback((s: ThemeStyle) => setStyleState(s), []);
  const setClarity = useCallback((c: GlassClarity) => setClarityState(c), []);
  const toggle = useCallback(
    () => setThemeState((t) => (t === "dark" ? "light" : "dark")),
    [],
  );
  return { theme, style, clarity, toggle, setTheme, setStyle, setClarity };
}

export function ThemeProvider({ theme, children }: { theme: Theme; children: ReactNode }) {
  return createElement(ThemeContext.Provider, { value: theme }, children);
}

export function useThemeValue(): Theme {
  return useContext(ThemeContext);
}
