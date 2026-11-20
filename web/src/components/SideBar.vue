<script setup lang="ts">
import type { Alias, DisplayAccount, FolderId, QuotaView } from "../types/mail";
import SvgIcon from "./SvgIcon.vue";
import FolderNav from "./FolderNav.vue";
import QuotaPanel from "./QuotaPanel.vue";

defineProps<{
  account: DisplayAccount | null;
  alias: Alias | null;
  folder: FolderId;
  inboxUnread: number;
  quota: QuotaView;
  open: boolean;
  composeDisabled: boolean;
}>();
const emit = defineEmits<{ openAccount: []; compose: []; selectFolder: [folder: FolderId] }>();
</script>

<template>
  <aside class="sidebar" :class="{ open }" aria-label="邮箱导航">
    <button class="account-trigger" aria-haspopup="dialog" @click="emit('openAccount')">
      <span class="avatar" :data-tone="account?.tone">{{ account?.initials }}</span>
      <span class="account-copy"
        ><strong>{{ account?.name }}</strong
        ><span>{{ alias?.email ?? "暂无邮箱地址" }}</span></span
      >
      <SvgIcon name="chevron" />
    </button>
    <button
      class="button primary compose-wide"
      :disabled="composeDisabled"
      @click="emit('compose')"
    >
      <SvgIcon name="compose" />写邮件
    </button>
    <FolderNav
      :folder="folder"
      :inbox-unread="inboxUnread"
      @select="emit('selectFolder', $event)"
    />
    <QuotaPanel :quota="quota" />
  </aside>
</template>
