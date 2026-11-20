/** 账号与地址的唯一数据源；me 只供额度与账号信息，别名不在 me 上同步。 */

import { computed, shallowRef, watch } from "vue";
import { ALIAS_LIMIT_FALLBACK, type MailApi } from "../api/mailApi";
import type {
  Alias,
  CreateAddressResult,
  DeleteAddressResult,
  DisplayAccount,
  Me,
  QuotaView,
} from "../types/mail";
import { decorateAccount } from "../utils/display";
import { errorMessage } from "../utils/error";
import { formatBytes } from "../utils/format";
import { t } from "./useHostContext";
import type { Notify } from "./useToasts";

function numeric(value: unknown): number | null {
  if (typeof value === "number") return value;
  if (typeof value === "string" && value.trim() !== "" && !Number.isNaN(Number(value)))
    return Number(value);
  return null;
}

function bytesLabel(value: unknown): string {
  const size = numeric(value);
  return size === null ? "—" : formatBytes(size);
}

export function useAccounts(api: MailApi, notify: Notify) {
  const accounts = shallowRef<DisplayAccount[]>([]);
  const accountId = shallowRef<string | null>(null);
  const aliasId = shallowRef<string | null>(null);
  const me = shallowRef<Me | null>(null);
  const accountsLoading = shallowRef(false);
  const accountsError = shallowRef<string | null>(null);
  const meLoading = shallowRef(false);

  let accountsToken = 0;
  let meToken = 0;

  const currentAccount = computed(
    () => accounts.value.find((item) => item.account === accountId.value) ?? null,
  );
  const currentAlias = computed<Alias | null>(
    () => currentAccount.value?.aliases.find((item) => item.alias_id === aliasId.value) ?? null,
  );
  const aliasLimit = computed(
    () => me.value?.account_info?.agent_alias_limit ?? ALIAS_LIMIT_FALLBACK,
  );

  const quotaView = computed<QuotaView>(() => {
    const empty: QuotaView = {
      dailySend: "—",
      perMinute: "—",
      perHour: "—",
      singleAttachment: "—",
      totalAttachment: "—",
    };
    const identity = me.value;
    if (!identity) return empty;
    const limits = identity.rate_limits;
    const constraints = identity.constraints;
    const daily = numeric(limits.daily_send_quota);
    const perMinute = numeric(limits.requests_per_minute);
    const perHour = numeric(limits.requests_per_hour);
    return {
      dailySend:
        daily === null
          ? "—"
          : t("quota.dailySendValue", { daily, sent: identity.sent_today_local }),
      perMinute: perMinute === null ? "—" : t("quota.perMinuteValue", { value: perMinute }),
      perHour: perHour === null ? "—" : t("quota.perHourValue", { value: perHour }),
      singleAttachment: bytesLabel(constraints.max_attachment_size_bytes),
      totalAttachment: bytesLabel(constraints.max_total_attachments_size_bytes),
    };
  });

  async function loadAccounts() {
    const token = ++accountsToken;
    accountsLoading.value = true;
    accountsError.value = null;
    try {
      const list = await api.listAccounts();
      if (token !== accountsToken) return;
      accounts.value = list.map(decorateAccount);
    } catch (error) {
      if (token !== accountsToken) return;
      accountsError.value = errorMessage(error);
    } finally {
      if (token === accountsToken) accountsLoading.value = false;
    }
  }

  async function loadMe(account: string) {
    const token = ++meToken;
    meLoading.value = true;
    try {
      const result = await api.me(account);
      if (token === meToken) me.value = result;
    } catch {
      if (token === meToken) me.value = null;
    } finally {
      if (token === meToken) meLoading.value = false;
    }
  }

  watch(
    accountId,
    (id) => {
      me.value = null;
      if (id) void loadMe(id);
    },
    { immediate: true },
  );

  function selectAddress(nextAccountId: string, nextAliasId: string) {
    accountId.value = nextAccountId;
    aliasId.value = nextAliasId;
  }

  function selectDefaultAccount() {
    const usable = accounts.value.find((item) => item.status === "ok" && item.aliases.length > 0);
    const target =
      usable ?? accounts.value.find((item) => item.status === "ok") ?? accounts.value[0] ?? null;
    if (!target) {
      accountId.value = null;
      aliasId.value = null;
      return;
    }
    accountId.value = target.account;
    const primary = target.aliases.find((item) => item.is_primary) ?? target.aliases[0] ?? null;
    aliasId.value = primary?.alias_id ?? null;
  }

  // 地址写响应只更新 accounts；刷新失败时重拉 accounts，再按需刷新当前账号的 me。
  async function applyAliasWrite(
    account: string,
    aliases: Alias[] | null,
    refreshRequired: boolean,
  ) {
    if (aliases && !refreshRequired) {
      accounts.value = accounts.value.map((item) =>
        item.account === account ? { ...item, aliases } : item,
      );
    } else {
      await loadAccounts();
    }
    if (account === accountId.value) await loadMe(account);
  }

  async function createAlias(account: string): Promise<CreateAddressResult> {
    const result = await api.createAddress(account);
    await applyAliasWrite(account, result.aliases, result.refresh_required);
    if (account === accountId.value && !currentAlias.value) {
      aliasId.value = result.alias.alias_id;
    }
    const email = result.alias.email ?? result.alias.alias_id;
    notify(
      result.reused
        ? t("mailboxDialog.reusedToast", { email })
        : t("mailboxDialog.createdToast", { email }),
    );
    if (result.refresh_error) notify(result.refresh_error);
    return result;
  }

  async function deleteAlias(
    account: string,
    removedAliasId: string,
  ): Promise<DeleteAddressResult> {
    const result = await api.deleteAddress(account, removedAliasId);
    await applyAliasWrite(account, result.aliases, result.refresh_required);
    if (account === accountId.value && aliasId.value === removedAliasId) {
      const target = accounts.value.find((item) => item.account === account);
      aliasId.value = target?.aliases[0]?.alias_id ?? null;
    }
    notify(t("mailboxDialog.deletedToast"));
    if (result.refresh_error) notify(result.refresh_error);
    return result;
  }

  return {
    accounts,
    accountId,
    aliasId,
    me,
    meLoading,
    accountsLoading,
    accountsError,
    currentAccount,
    currentAlias,
    aliasLimit,
    quotaView,
    loadAccounts,
    loadMe,
    selectAddress,
    selectDefaultAccount,
    createAlias,
    deleteAlias,
  };
}

export type Accounts = ReturnType<typeof useAccounts>;
