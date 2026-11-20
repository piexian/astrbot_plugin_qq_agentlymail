<script setup lang="ts">
import { computed } from "vue";
import type { Alias, DisplayAccount } from "../types/mail";
import DialogShell from "./DialogShell.vue";
import SvgIcon from "./SvgIcon.vue";

const props = defineProps<{
  open: boolean;
  account: DisplayAccount | null;
  aliasLimit: number;
  loading: boolean;
  meLoading: boolean;
}>();
const emit = defineEmits<{ close: []; create: []; deleteAlias: [alias: Alias] }>();

const count = computed(() => props.account?.aliases.length ?? 0);
const subtitle = computed(() =>
  props.account ? `${props.account.name} · ${count.value} / ${props.aliasLimit} 个地址` : "",
);
const full = computed(() => count.value >= props.aliasLimit);
</script>

<template>
  <DialogShell
    :open="open"
    title="邮箱地址"
    :subtitle="subtitle"
    dialog-class="mailbox-dialog"
    @close="emit('close')"
  >
    <p class="field-hint mailbox-note">新建地址的前缀由服务端派生，不支持自定义；注销不可恢复。</p>
    <p v-if="meLoading" class="field-hint mailbox-note">正在读取账号额度…</p>
    <div class="mailbox-list">
      <div v-for="alias in account?.aliases ?? []" :key="alias.alias_id" class="mailbox-row">
        <span class="avatar small" :data-tone="account?.tone">{{
          (alias.email ?? alias.alias_id).slice(0, 1).toUpperCase()
        }}</span>
        <div>
          <strong>{{ alias.email ?? alias.alias_id }}</strong>
          <p>{{ alias.is_primary ? "主地址" : "邮箱地址" }}</p>
        </div>
        <button class="button secondary" :disabled="loading" @click="emit('deleteAlias', alias)">
          注销
        </button>
      </div>
      <p v-if="count === 0" class="mailbox-empty">暂无邮箱地址，点击下方新建。</p>
    </div>
    <template #footer>
      <button class="button secondary" :disabled="full || loading" @click="emit('create')">
        <SvgIcon v-if="!full && !loading" name="plus" />{{
          loading ? "新建中…" : full ? `已达 ${aliasLimit} 个地址` : "新建邮箱地址"
        }}
      </button>
      <button class="button primary" @click="emit('close')">完成</button>
    </template>
  </DialogShell>
</template>
