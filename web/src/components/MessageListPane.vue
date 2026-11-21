<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "../composables/useHostContext";
import type { Alias, DisplayAccount, FolderId, MailFilter, MessageSummary } from "../types/mail";
import { accountStatusLabel } from "../utils/display";
import { timeLabel } from "../utils/format";
import SvgIcon from "./SvgIcon.vue";

const props = defineProps<{
  account: DisplayAccount | null;
  alias: Alias | null;
  folder: FolderId;
  messages: MessageSummary[];
  selectedId: string | null;
  summary: string;
  loading: boolean;
  loadingMore: boolean;
  hasMore: boolean;
  error: string | null;
  searchMode: boolean;
  accountsLoading: boolean;
  accountsError: string | null;
  accountCount: number;
}>();
const emit = defineEmits<{
  selectMessage: [messageId: string];
  openAccount: [];
  openLogin: [accountId?: string];
  loadMore: [];
  retry: [];
  retryAccounts: [];
  clearSearch: [];
}>();
const filter = defineModel<MailFilter>("filter", { required: true });
const { t, locale } = useI18n();

const filters = computed<{ id: MailFilter; label: string }[]>(() => {
  // 已发送没有读状态，不提供未读筛选。
  if (props.folder === "sent") {
    return [{ id: "all", label: t("list.filterAll") }];
  }
  return [
    { id: "all", label: t("list.filterAll") },
    { id: "unread", label: t("list.filterUnread") },
  ];
});

const paneState = computed(() => {
  if (props.accountsLoading) return "accounts-loading";
  if (props.accountsError) return "accounts-error";
  if (props.accountCount === 0 || !props.account) return "no-accounts";
  if (props.account.status !== "ok") return "account-invalid";
  if (!props.alias) return "no-alias";
  if (props.loading) return "list-loading";
  if (props.error && props.messages.length === 0) return "list-error";
  if (props.messages.length === 0) return "empty";
  return "list";
});

const emptyTitle = computed(() =>
  props.searchMode ? t("list.emptySearchTitle") : t("list.emptyTitle"),
);
const emptyText = computed(() =>
  props.searchMode ? t("list.emptySearchText") : t("list.emptyText"),
);
</script>

<template>
  <main class="message-list-pane">
    <div class="list-header-bar">
      <div v-if="searchMode" class="list-header-left">
        <span class="list-search-badge">{{ t("list.searchResults") }}</span>
        <button type="button" class="list-clear-btn" @click="emit('clearSearch')">
          <SvgIcon name="close" />
          <span>{{ t("list.clearSearch") }}</span>
        </button>
      </div>
      <div v-else class="list-filters" role="group" :aria-label="t('list.filterGroup')">
        <button
          v-for="item in filters"
          :key="item.id"
          type="button"
          class="filter"
          :class="{ active: filter === item.id }"
          @click="filter = item.id"
        >
          {{ item.label }}
        </button>
      </div>

      <div class="list-header-right">
        <span v-if="paneState === 'list'" class="list-count" :title="summary"
          >{{ messages.length }}{{ hasMore ? "+" : "" }}</span
        >
      </div>
    </div>

    <div class="message-list" :aria-label="t('list.ariaLabel')">
      <div v-if="paneState === 'accounts-loading'" class="empty-list">
        <SvgIcon name="refresh" /><strong>{{ t("list.loadingAccounts") }}</strong>
      </div>
      <div v-else-if="paneState === 'accounts-error'" class="empty-list">
        <SvgIcon name="mail" /><strong>{{ t("list.accountsErrorTitle") }}</strong>
        <p>{{ accountsError }}</p>
        <button type="button" class="button secondary" @click="emit('retryAccounts')">
          {{ t("list.retry") }}
        </button>
      </div>
      <div v-else-if="paneState === 'no-accounts'" class="empty-list">
        <SvgIcon name="mail" /><strong>{{ t("list.noAccountsTitle") }}</strong>
        <p>{{ t("list.noAccountsHint") }}</p>
        <div class="empty-actions">
          <button type="button" class="button primary" @click="emit('openLogin', 'default')">
            {{ t("login.title") }}
          </button>
          <button type="button" class="button secondary" @click="emit('openAccount')">
            {{ t("list.viewAccounts") }}
          </button>
        </div>
      </div>
      <div v-else-if="paneState === 'account-invalid'" class="empty-list">
        <SvgIcon name="mail" /><strong>{{
          account ? accountStatusLabel(account.status) : ""
        }}</strong>
        <p>{{ account?.error ?? t("list.accountUnavailable") }}</p>
        <div class="empty-actions">
          <button type="button" class="button primary" @click="emit('openLogin', account?.account)">
            {{ t("accountDialog.relogin") }}
          </button>
          <button type="button" class="button secondary" @click="emit('openAccount')">
            {{ t("list.switchAccount") }}
          </button>
        </div>
      </div>
      <div v-else-if="paneState === 'no-alias'" class="empty-list">
        <SvgIcon name="mail" /><strong>{{ t("sidebar.noAddress") }}</strong>
        <p>{{ t("list.noAliasHint") }}</p>
        <button type="button" class="button secondary" @click="emit('openAccount')">
          {{ t("list.manageAddresses") }}
        </button>
      </div>
      <div v-else-if="paneState === 'list-loading'" class="empty-list">
        <SvgIcon name="refresh" /><strong>{{ t("list.loadingMessages") }}</strong>
      </div>
      <div v-else-if="paneState === 'list-error'" class="empty-list">
        <SvgIcon name="mail" /><strong>{{ t("list.listErrorTitle") }}</strong>
        <p>{{ error }}</p>
        <button type="button" class="button secondary" @click="emit('retry')">
          {{ t("list.retry") }}
        </button>
      </div>
      <div v-else-if="paneState === 'empty'" class="empty-list">
        <SvgIcon name="mail" /><strong>{{ emptyTitle }}</strong>
        <p>{{ emptyText }}</p>
      </div>
      <template v-else>
        <p v-if="error" class="list-error-banner" role="alert">
          {{ error }}
          <button type="button" class="button secondary" @click="emit('retry')">
            {{ t("list.retry") }}
          </button>
        </p>
        <button
          v-for="message in messages"
          :key="message.message_id"
          type="button"
          class="message-row"
          :class="{
            unread: folder !== 'sent' && !message.is_read,
            selected: message.message_id === selectedId,
          }"
          @click="emit('selectMessage', message.message_id)"
        >
          <span class="message-top">
            <span
              v-if="folder !== 'sent'"
              class="unread-dot"
              :class="{ placeholder: message.is_read }"
            ></span>
            <span class="message-sender">{{ message.from_?.name ?? message.from_?.email }}</span>
            <span class="message-time">{{
              message.created_at ? timeLabel(message.created_at, locale) : ""
            }}</span>
          </span>
          <span class="message-subject">{{ message.subject }}</span>
        </button>
      </template>
    </div>

    <div class="list-footer">
      <span>{{ summary }}</span>
      <button
        v-if="paneState === 'list' && hasMore"
        type="button"
        class="button secondary load-more"
        :disabled="loadingMore"
        @click="emit('loadMore')"
      >
        {{ loadingMore ? t("list.loadingMore") : t("list.loadMore") }}
      </button>
      <span v-else-if="paneState === 'list'">{{ t("list.noMore") }}</span>
    </div>
  </main>
</template>
