/** 同步 AstrBot WebUI 的语言（locale/i18n）与深浅色主题（isDark / ?theme=）。 */

import { readonly, shallowRef } from "vue";
import type { AstrBotBridge, BridgeContext } from "../api/bridge";
import { MESSAGES, type MessageKey, type SupportedLocale } from "../i18n/messages";

const THEME_COLORS = {
  light: "#f3f5f8",
  dark: "#11161d",
} as const;

export function normalizeLocale(raw?: string | null): SupportedLocale {
  if (!raw || typeof raw !== "string") return "zh-CN";
  const value = raw.trim().toLowerCase().replace("_", "-");
  if (!value) return "zh-CN";
  if (value.startsWith("zh")) return "zh-CN";
  if (value.startsWith("ja")) return "ja-JP";
  if (value.startsWith("ru")) return "ru-RU";
  if (value.startsWith("en")) return "en-US";
  return "en-US";
}

function initialThemeDark(): boolean {
  const param = new URLSearchParams(window.location.search).get("theme");
  if (param === "dark") return true;
  if (param === "light") return false;
  return document.documentElement.getAttribute("data-theme") === "dark";
}

function initialLocale(): SupportedLocale {
  const param = new URLSearchParams(window.location.search).get("locale");
  if (param) return normalizeLocale(param);
  return "zh-CN";
}

const isDark = shallowRef<boolean>(initialThemeDark());
const locale = shallowRef<SupportedLocale>(initialLocale());
const hostI18n = shallowRef<Record<string, unknown>>({});

function applyDocumentTheme(dark: boolean): void {
  isDark.value = dark;
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) {
    meta.setAttribute("content", dark ? THEME_COLORS.dark : THEME_COLORS.light);
  }
}

function applyDocumentLocale(next: SupportedLocale): void {
  locale.value = next;
  document.documentElement.lang = next;
  document.title = MESSAGES[next]["app.title"];
}

applyDocumentTheme(isDark.value);
applyDocumentLocale(locale.value);

export function applyHostContext(ctx: BridgeContext | null | undefined): void {
  if (!ctx || typeof ctx !== "object") return;
  if (typeof ctx.isDark === "boolean") {
    applyDocumentTheme(ctx.isDark);
  }
  if (ctx.i18n && typeof ctx.i18n === "object") {
    hostI18n.value = ctx.i18n;
  }
  if (typeof ctx.locale === "string" && ctx.locale.trim()) {
    applyDocumentLocale(normalizeLocale(ctx.locale));
  }
}

export function syncHostContext(bridge: AstrBotBridge | null): () => void {
  if (!bridge) {
    const param = new URLSearchParams(window.location.search).get("theme");
    if (param !== "dark" && param !== "light" && typeof window.matchMedia === "function") {
      const media = window.matchMedia("(prefers-color-scheme: dark)");
      applyDocumentTheme(media.matches);
      const onChange = (event: MediaQueryListEvent) => applyDocumentTheme(event.matches);
      media.addEventListener("change", onChange);
      return () => media.removeEventListener("change", onChange);
    }
    return () => {};
  }

  applyHostContext(bridge.getContext?.());
  const unsubscribe = bridge.onContext?.((ctx) => {
    applyHostContext(ctx);
  });
  return () => {
    unsubscribe?.();
  };
}

function lookupHostString(lang: SupportedLocale, key: string): string | null {
  const table = hostI18n.value[lang];
  if (!table || typeof table !== "object") return null;
  const strings = (table as Record<string, unknown>).strings;
  if (!strings || typeof strings !== "object") return null;
  const hit = (strings as Record<string, unknown>)[key];
  return typeof hit === "string" ? hit : null;
}

export function t(key: MessageKey, vars?: Record<string, string | number>): string {
  const lang = locale.value;
  const template =
    lookupHostString(lang, key) ??
    MESSAGES[lang][key] ??
    MESSAGES["en-US"][key] ??
    MESSAGES["zh-CN"][key] ??
    key;
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (match, name: string) =>
    Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : match,
  );
}

export function useI18n() {
  return {
    locale: readonly(locale),
    isDark: readonly(isDark),
    t,
  };
}
