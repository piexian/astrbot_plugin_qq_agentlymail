<script setup lang="ts">
import type { Alias } from "../types/mail";
import DialogShell from "./DialogShell.vue";

defineProps<{ open: boolean; alias: Alias | null; accountName: string; loading: boolean }>();
const emit = defineEmits<{ close: []; confirm: [] }>();
</script>

<template>
  <DialogShell
    :open="open"
    title="注销邮箱地址"
    :subtitle="`${accountName} · 不可恢复操作`"
    dialog-class="small-dialog"
    footer-end
    @close="emit('close')"
  >
    <div class="dialog-body">
      <strong>{{ alias?.email ?? alias?.alias_id }}</strong>
      <ul class="risk-list">
        <li>该地址下的全部邮件将被删除，无法恢复。</li>
        <li>绑定该地址的 Agent 将被解绑。</li>
        <li>地址进入 90 天保留期，期内新建地址会复用该邮箱。</li>
      </ul>
      <p class="confirm-note">确认后将在真实服务上注销该地址。</p>
    </div>
    <template #footer>
      <button class="button secondary" :disabled="loading" @click="emit('close')">取消</button>
      <button class="button danger" :disabled="loading" @click="emit('confirm')">
        {{ loading ? "注销中…" : "确认注销" }}
      </button>
    </template>
  </DialogShell>
</template>
