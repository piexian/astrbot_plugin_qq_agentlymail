<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "../composables/useHostContext";
import type { Alias, DisplayAccount, FolderId, MailFilter, MessageSummary } from "../types/mail";
import { accountStatusLabel, folderLabel } from "../utils/display";
import { timeLabel } from "../utils/format";
import SvgIcon from "./SvgIcon.vue";

const props = defineProps<{
  account: DisplayAccount | null;
  alias: Alias | null;
  folder: FolderId;
  messages: MessageSummary[];
  selectedId: string | null;
  summary: string;
  composeDisabled: boolean;
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
  compose: [];
  loadMore: [];
  retry: [];
  retryAccounts: [];
}>();
const filter = defineModel<MailFilter>("filter", { required: true });
const { t, locale } = useI18n();

const filters = computed(() => [
  { id: "all" as MailFilter, label: t("list.filterAll"), disabled: false, title: "" },
  {
    id: "unread" as MailFilter,
    label: t("list.filterUnread"),
    disabled: props.searchMode,
    title: props.searchMode ? t("list.filterUnreadDisabledTitle") : "",
  },
  {
    id: "attachment" as MailFilter,
    label: t("list.filterAttachment"),
    disabled: true,
    title: t("list.filterAttachmentTitle"),
  },
]);

const title = computed(() =>
  props.searchMode ? t("list.searchResults") : folderLabel(props.folder),
);

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
    <div class="mobile-account-row mobile-only">
      <button @click="emit('openAccount')">
        <span class="avatar small" :data-tone="account?.tone">{{ account?.initials }}</span>
        <span>{{ alias?.email ?? t("sidebar.noAddress") }}</span>
        <SvgIcon name="chevron" />
      </button>
    </div>
    <div class="list-heading">
      <div>
        <h1>{{ title }}</h1>
        <p>{{ summary }}</p>
      </div>
      <button
        class="button secondary mobile-only"
        :disabled="composeDisabled"
        @click="emit('compose')"
      >
        {{ t("topbar.compose") }}
      </button>
    </div>
    <div class="list-filters" role="group" :aria-label="t('list.filterGroup')">
      <button
        v-for="item in filters"
        :key="item.id"
        class="filter"
        :class="{ active: filter === item.id }"
        :disabled="item.disabled"
        :title="item.title"
        @click="filter = item.id"
      >
        {{ item.label }}
      </button>
    </div>
    <p v-if="searchMode" class="field-hint filter-hint">
      {{ t("list.searchHint") }}
    </p>
    <p v-else class="field-hint filter-hint">{{ t("list.attachmentHint") }}</p>

    <div class="message-list" :aria-label="t('list.ariaLabel')">
      <div v-if="paneState === 'accounts-loading'" class="empty-list">
        <SvgIcon name="refresh" /><strong>{{ t("list.loadingAccounts") }}</strong>
      </div>
      <div v-else-if="paneState === 'accounts-error'" class="empty-list">
        <SvgIcon name="mail" /><strong>{{ t("list.accountsErrorTitle") }}</strong>
        <p>{{ accountsError }}</p>
        <button class="button secondary" @click="emit('retryAccounts')">
          {{ t("list.retry") }}
        </button>
      </div>
      <div v-else-if="paneState === 'no-accounts'" class="empty-list">
        <SvgIcon name="mail" /><strong>{{ t("list.noAccountsTitle") }}</strong>
        <p>{{ t("list.noAccountsHint") }}</p>
        <div class="empty-actions">
          <button class="button primary" @click="emit('openLogin', 'default')">
            {{ t("login.title") }}
          </button>
          <button class="button secondary" @click="emit('openAccount')">
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
          <button class="button primary" @click="emit('openLogin', account?.account)">
            {{ t("accountDialog.relogin") }}
          </button>
          <button class="button secondary" @click="emit('openAccount')">
            {{ t("list.switchAccount") }}
          </button>
        </div>
      </div>
      <div v-else-if="paneState === 'no-alias'" class="empty-list">
        <SvgIcon name="mail" /><strong>{{ t("sidebar.noAddress") }}</strong>
        <p>{{ t("list.noAliasHint") }}</p>
        <button class="button secondary" @click="emit('openAccount')">
          {{ t("list.manageAddresses") }}
        </button>
      </div>
      <div v-else-if="paneState === 'list-loading'" class="empty-list">
        <SvgIcon name="refresh" /><strong>{{ t("list.loadingMessages") }}</strong>
      </div>
      <div v-else-if="paneState === 'list-error'" class="empty-list">
        <SvgIcon name="mail" /><strong>{{ t("list.listErrorTitle") }}</strong>
        <p>{{ error }}</p>
        <button class="button secondary" @click="emit('retry')">{{ t("list.retry") }}</button>
      </div>
      <div v-else-if="paneState === 'empty'" class="empty-list">
        <SvgIcon name="mail" /><strong>{{ emptyTitle }}</strong>
        <p>{{ emptyText }}</p>
      </div>
      <template v-else>
        <p v-if="error" class="list-error-banner" role="alert">
          {{ error }}
          <button class="button secondary" @click="emit('retry')">{{ t("list.retry") }}</button>
        </p>
        <button
          v-for="message in messages"
          :key="message.message_id"
          class="message-row"
          :class="{ unread: !message.is_read, selected: message.message_id === selectedId }"
          @click="emit('selectMessage', message.message_id)"
        >
          <span class="message-top">
            <span class="unread-dot" :class="{ placeholder: message.is_read }"></span>
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
