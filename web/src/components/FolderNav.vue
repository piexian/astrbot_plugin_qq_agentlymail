<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "../composables/useHostContext";
import type { FolderId } from "../types/mail";
import { folderLabel } from "../utils/display";
import SvgIcon from "./SvgIcon.vue";

defineProps<{ folder: FolderId; inboxUnread: number }>();
const emit = defineEmits<{ select: [folder: FolderId] }>();
const { t } = useI18n();

const folders = computed<{ id: FolderId; label: string; icon: string }[]>(() => [
  { id: "inbox", label: folderLabel("inbox"), icon: "inbox" },
  { id: "sent", label: folderLabel("sent"), icon: "send" },
  { id: "spam", label: folderLabel("spam"), icon: "spam" },
  { id: "trash", label: folderLabel("trash"), icon: "trash" },
]);
</script>

<template>
  <nav class="folder-nav" :aria-label="t('folder.navLabel')">
    <p class="nav-label">{{ t("folder.sectionTitle") }}</p>
    <button
      v-for="item in folders"
      :key="item.id"
      class="folder"
      :class="{ active: folder === item.id }"
      @click="emit('select', item.id)"
    >
      <SvgIcon :name="item.icon" /><span>{{ item.label }}</span>
      <span v-if="item.id === 'inbox' && inboxUnread > 0" class="folder-count">{{
        inboxUnread
      }}</span>
    </button>
  </nav>
</template>
