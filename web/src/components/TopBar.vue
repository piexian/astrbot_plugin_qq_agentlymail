<script setup lang="ts">
/** 顶栏：集成账号/地址切换、目录 Tab 栏、全局搜索、折叠额度浮层、刷新与写邮件。 */
import { onMounted, onUnmounted, useTemplateRef } from "vue";
import { useI18n } from "../composables/useHostContext";
import type { Alias, DisplayAccount, FolderId, QuotaView } from "../types/mail";
import FolderNav from "./FolderNav.vue";
import QuotaPanel from "./QuotaPanel.vue";
import SvgIcon from "./SvgIcon.vue";

defineProps<{
  account: DisplayAccount | null;
  alias: Alias | null;
  folder: FolderId;
  searchMode: boolean;
  inboxUnread: number;
  quota: QuotaView;
  composeDisabled: boolean;
}>();
const emit = defineEmits<{
  openAccount: [];
  selectFolder: [folder: FolderId];
  compose: [];
  refresh: [];
}>();
const query = defineModel<string>({ required: true });
const searchInput = useTemplateRef<HTMLInputElement>("search");
const { t } = useI18n();

function onKeydown(event: KeyboardEvent) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    searchInput.value?.focus();
  }
}

onMounted(() => document.addEventListener("keydown", onKeydown));
onUnmounted(() => document.removeEventListener("keydown", onKeydown));
</script>

<template>
  <header class="topbar">
    <div class="topbar-main">
      <button
        type="button"
        class="account-trigger topbar-account"
        aria-haspopup="dialog"
        :aria-label="t('topbar.switchAccount')"
        @click="emit('openAccount')"
      >
        <span class="avatar small" :data-tone="account?.tone">{{ account?.initials ?? "?" }}</span>
        <span class="account-copy">
          <strong>{{ account?.name ?? t("app.title") }}</strong>
          <span>{{ alias?.email ?? t("sidebar.noAddress") }}</span>
        </span>
        <SvgIcon name="chevron" />
      </button>

      <div class="topbar-tabs-inline">
        <FolderNav
          :folder="folder"
          :search-mode="searchMode"
          :inbox-unread="inboxUnread"
          @select="emit('selectFolder', $event)"
        />
      </div>

      <label class="global-search">
        <SvgIcon name="search" />
        <input
          ref="search"
          v-model="query"
          type="search"
          :placeholder="t('topbar.searchPlaceholder')"
          autocomplete="off"
        />
        <kbd>Ctrl K</kbd>
      </label>

      <div class="topbar-actions">
        <QuotaPanel :quota="quota" />
        <button
          type="button"
          class="icon-button"
          :aria-label="t('topbar.refresh')"
          :title="t('topbar.refresh')"
          @click="emit('refresh')"
        >
          <SvgIcon name="refresh" />
        </button>
        <button
          type="button"
          class="button primary topbar-compose"
          :disabled="composeDisabled"
          :aria-label="t('topbar.compose')"
          :title="t('topbar.compose')"
          @click="emit('compose')"
        >
          <SvgIcon name="compose" /><span>{{ t("topbar.compose") }}</span>
        </button>
      </div>
    </div>

    <div class="topbar-tabs-bar">
      <FolderNav
        :folder="folder"
        :search-mode="searchMode"
        :inbox-unread="inboxUnread"
        @select="emit('selectFolder', $event)"
      />
    </div>
  </header>
</template>
