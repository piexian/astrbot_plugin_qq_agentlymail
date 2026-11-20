<script setup lang="ts">
import type { FolderId } from "../types/mail";
import SvgIcon from "./SvgIcon.vue";

defineProps<{ folder: FolderId; inboxUnread: number }>();
const emit = defineEmits<{ select: [folder: FolderId] }>();

const folders: { id: FolderId; label: string; icon: string }[] = [
  { id: "inbox", label: "收件箱", icon: "inbox" },
  { id: "sent", label: "已发送", icon: "send" },
  { id: "spam", label: "垃圾邮件", icon: "spam" },
  { id: "trash", label: "垃圾箱", icon: "trash" },
];
</script>

<template>
  <nav class="folder-nav" aria-label="邮件文件夹">
    <p class="nav-label">邮件</p>
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
