/** 展示用格式化函数；时间按 UTC+8 渲染，相对与日期标签随当前语言切换。 */

import { t, useI18n } from "../composables/useHostContext";
import type { SupportedLocale } from "../i18n/messages";

const OFFSET_MS = 8 * 60 * 60 * 1000;

interface DateParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  shifted: Date;
}

function parts(iso: string): DateParts {
  const shifted = new Date(new Date(iso).getTime() + OFFSET_MS);
  return {
    year: shifted.getUTCFullYear(),
    month: shifted.getUTCMonth() + 1,
    day: shifted.getUTCDate(),
    hour: shifted.getUTCHours(),
    minute: shifted.getUTCMinutes(),
    shifted,
  };
}

function dayKey(value: DateParts): number {
  return Date.UTC(value.year, value.month - 1, value.day);
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function formatMonthDay(date: DateParts, lang: SupportedLocale): string {
  if (lang === "zh-CN") return `${date.month} 月 ${date.day} 日`;
  if (lang === "ja-JP") return `${date.month}月${date.day}日`;
  return new Intl.DateTimeFormat(lang, {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  }).format(date.shifted);
}

function formatYearMonthDay(date: DateParts, lang: SupportedLocale): string {
  if (lang === "zh-CN") return `${date.year} 年 ${date.month} 月 ${date.day} 日`;
  if (lang === "ja-JP") return `${date.year}年${date.month}月${date.day}日`;
  return new Intl.DateTimeFormat(lang, {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  }).format(date.shifted);
}

export function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) {
    const mb = bytes / 1024 / 1024;
    return (Number.isInteger(mb) ? String(mb) : mb.toFixed(1)) + " MB";
  }
  if (bytes >= 1024) return Math.ceil(bytes / 1024) + " KB";
  return bytes + " B";
}

export function timeLabel(iso: string, lang?: SupportedLocale): string {
  const activeLocale = lang ?? useI18n().locale.value;
  const nowMs = Date.now();
  const now = parts(new Date(nowMs).toISOString());
  const date = parts(iso);
  const diffMs = nowMs - new Date(iso).getTime();
  if (diffMs >= 0 && diffMs < 60_000) return t("time.justNow");
  const days = (dayKey(now) - dayKey(date)) / 86_400_000;
  if (days === 0) return `${pad(date.hour)}:${pad(date.minute)}`;
  if (days === 1) return t("time.yesterday");
  return formatMonthDay(date, activeLocale);
}

export function fullDateLabel(iso: string, lang?: SupportedLocale): string {
  const activeLocale = lang ?? useI18n().locale.value;
  const date = parts(iso);
  const base = formatYearMonthDay(date, activeLocale);
  const diffMs = Date.now() - new Date(iso).getTime();
  if (diffMs >= 0 && diffMs < 60_000) return `${base} · ${t("time.justNow")}`;
  return `${base} ${pad(date.hour)}:${pad(date.minute)}`;
}
