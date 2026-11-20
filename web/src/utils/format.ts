/** 展示用格式化函数；时间按 UTC+8 渲染，相对标签用真实当前时间。 */

const OFFSET_MS = 8 * 60 * 60 * 1000;

interface DateParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
}

function parts(iso: string): DateParts {
  const shifted = new Date(new Date(iso).getTime() + OFFSET_MS);
  return {
    year: shifted.getUTCFullYear(),
    month: shifted.getUTCMonth() + 1,
    day: shifted.getUTCDate(),
    hour: shifted.getUTCHours(),
    minute: shifted.getUTCMinutes(),
  };
}

function dayKey(value: DateParts): number {
  return Date.UTC(value.year, value.month - 1, value.day);
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

export function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) {
    const mb = bytes / 1024 / 1024;
    return (Number.isInteger(mb) ? String(mb) : mb.toFixed(1)) + " MB";
  }
  if (bytes >= 1024) return Math.ceil(bytes / 1024) + " KB";
  return bytes + " B";
}

export function timeLabel(iso: string): string {
  const nowMs = Date.now();
  const now = parts(new Date(nowMs).toISOString());
  const date = parts(iso);
  const diffMs = nowMs - new Date(iso).getTime();
  if (diffMs >= 0 && diffMs < 60_000) return "刚刚";
  const days = (dayKey(now) - dayKey(date)) / 86_400_000;
  if (days === 0) return `${pad(date.hour)}:${pad(date.minute)}`;
  if (days === 1) return "昨天";
  return `${date.month} 月 ${date.day} 日`;
}

export function fullDateLabel(iso: string): string {
  const date = parts(iso);
  const base = `${date.year} 年 ${date.month} 月 ${date.day} 日`;
  const diffMs = Date.now() - new Date(iso).getTime();
  if (diffMs >= 0 && diffMs < 60_000) return `${base} · 刚刚`;
  return `${base} ${pad(date.hour)}:${pad(date.minute)}`;
}
