/** 账号展示装饰与多语言标签映射：由 account/email 本地派生名称、首字母与色调。 */

import { t } from "../composables/useHostContext";
import type { AccountStatus, AccountView, DisplayAccount, FolderId, Tone } from "../types/mail";

const TONES: Tone[] = ["blue", "green", "orange", "slate"];

export function accountStatusLabel(status: AccountStatus): string {
  switch (status) {
    case "ok":
      return t("status.ok");
    case "invalid_credentials":
      return t("status.invalid_credentials");
    case "auth_expired":
      return t("status.auth_expired");
    case "error":
      return t("status.error");
  }
}

export function folderLabel(folder: FolderId): string {
  switch (folder) {
    case "inbox":
      return t("folder.inbox");
    case "sent":
      return t("folder.sent");
    case "spam":
      return t("folder.spam");
    case "trash":
      return t("folder.trash");
  }
}

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
  };
}

export function senderTone(email: string): Tone {
  return toneOf(email);
}
