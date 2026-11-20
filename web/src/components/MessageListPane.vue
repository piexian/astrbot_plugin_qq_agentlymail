<script setup lang="ts">
import { computed } from "vue";
import type { Alias, DisplayAccount, FolderId, MailFilter, MessageSummary } from "../types/mail";
import { FOLDER_NAMES } from "../types/mail";
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
  compose: [];
  loadMore: [];
  retry: [];
  retryAccounts: [];
}>();
const filter = defineModel<MailFilter>("filter", { required: true });

const filters = computed(() => [
  { id: "all" as MailFilter, label: "全部", disabled: false, title: "" },
  {
    id: "unread" as MailFilter,
    label: "未读",
    disabled: props.searchMode,
    title: props.searchMode ? "搜索时不支持未读筛选" : "",
  },
  {
    id: "attachment" as MailFilter,
    label: "有附件",
    disabled: true,
    title: "列表不含附件信息，无法按附件筛选",
  },
]);

const title = computed(() => (props.searchMode ? "搜索结果" : FOLDER_NAMES[props.folder]));

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

const emptyTitle = computed(() => (props.searchMode ? "没有匹配的邮件" : "这里没有邮件"));
const emptyText = computed(() =>
  props.searchMode ? "换个关键词试试。" : "切换文件夹或调整筛选条件。",
);
</script>

<template>
  <main class="message-list-pane">
    <div class="mobile-account-row mobile-only">
      <button @click="emit('openAccount')">
        <span class="avatar small" :data-tone="account?.tone">{{ account?.initials }}</span>
        <span>{{ alias?.email ?? "暂无邮箱地址" }}</span>
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
        写邮件
      </button>
    </div>
    <div class="list-filters" role="group" aria-label="邮件筛选">
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
      搜索范围为当前地址的全部邮件，暂不支持筛选。
    </p>
    <p v-else class="field-hint filter-hint">列表不含附件信息，“有附件”筛选不可用。</p>

    <div class="message-list" aria-label="邮件列表">
      <div v-if="paneState === 'accounts-loading'" class="empty-list">
        <SvgIcon name="refresh" /><strong>正在加载账号…</strong>
      </div>
      <div v-else-if="paneState === 'accounts-error'" class="empty-list">
        <SvgIcon name="mail" /><strong>账号加载失败</strong>
        <p>{{ accountsError }}</p>
        <button class="button secondary" @click="emit('retryAccounts')">重试</button>
      </div>
      <div v-else-if="paneState === 'no-accounts'" class="empty-list">
        <SvgIcon name="mail" /><strong>还没有邮箱账号</strong>
        <p>请在聊天中执行 /agently login 添加账号。</p>
        <button class="button secondary" @click="emit('openAccount')">查看账号</button>
      </div>
      <div v-else-if="paneState === 'account-invalid'" class="empty-list">
        <SvgIcon name="mail" /><strong>{{ account?.statusLabel }}</strong>
        <p>{{ account?.error ?? "该账号当前不可用。" }}</p>
        <button class="button secondary" @click="emit('openAccount')">切换账号</button>
      </div>
      <div v-else-if="paneState === 'no-alias'" class="empty-list">
        <SvgIcon name="mail" /><strong>暂无邮箱地址</strong>
        <p>打开账号切换，在地址管理中新建地址。</p>
        <button class="button secondary" @click="emit('openAccount')">管理地址</button>
      </div>
      <div v-else-if="paneState === 'list-loading'" class="empty-list">
        <SvgIcon name="refresh" /><strong>正在加载邮件…</strong>
      </div>
      <div v-else-if="paneState === 'list-error'" class="empty-list">
        <SvgIcon name="mail" /><strong>邮件加载失败</strong>
        <p>{{ error }}</p>
        <button class="button secondary" @click="emit('retry')">重试</button>
      </div>
      <div v-else-if="paneState === 'empty'" class="empty-list">
        <SvgIcon name="mail" /><strong>{{ emptyTitle }}</strong>
        <p>{{ emptyText }}</p>
      </div>
      <template v-else>
        <p v-if="error" class="list-error-banner" role="alert">
          {{ error }}
          <button class="button secondary" @click="emit('retry')">重试</button>
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
              message.created_at ? timeLabel(message.created_at) : ""
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
        {{ loadingMore ? "加载中…" : "加载更多" }}
      </button>
      <span v-else-if="paneState === 'list'">没有更多了</span>
    </div>
  </main>
</template>
