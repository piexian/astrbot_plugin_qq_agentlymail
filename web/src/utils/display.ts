/** 账号展示装饰：由 account/email 本地派生名称、首字母与色调。 */

import type { AccountStatus, AccountView, DisplayAccount, Tone } from "../types/mail";

const TONES: Tone[] = ["blue", "green", "orange", "slate"];

const STATUS_LABELS: Record<AccountStatus, string> = {
  ok: "已授权",
  invalid_credentials: "凭证无效",
  auth_expired: "授权失效",
  error: "读取失败",
};

function toneOf(seed: string): Tone {
  let hash = 0;
  for (const char of seed) hash = (hash * 31 + (char.codePointAt(0) ?? 0)) % 997;
  return TONES[hash % TONES.length];
}

export function decorateAccount(view: AccountView): DisplayAccount {
  const name = view.account;
  return {
    ...view,
    aliases: view.aliases ?? [],
    name,
    initials: (name.trim()[0] ?? "?").toUpperCase(),
    tone: toneOf(name),
    statusLabel: STATUS_LABELS[view.status],
  };
}

export function senderTone(email: string): Tone {
  return toneOf(email);
}
