<script setup lang="ts">
import type { DisplayAccount } from "../types/mail";
import DialogShell from "./DialogShell.vue";
import SvgIcon from "./SvgIcon.vue";

defineProps<{
  open: boolean;
  accounts: DisplayAccount[];
  currentAccountId: string | null;
  currentAliasId: string | null;
}>();
const emit = defineEmits<{
  close: [];
  selectAddress: [accountId: string, aliasId: string];
  manage: [accountId: string];
  addAccount: [];
}>();
</script>

<template>
  <DialogShell
    :open="open"
    title="切换账号与邮箱"
    subtitle="凭证按账号隔离，地址共享账号额度"
    dialog-class="account-dialog"
    @close="emit('close')"
  >
    <div class="account-list">
      <section v-for="account in accounts" :key="account.account" class="account-group">
        <div class="account-group-header">
          <span class="avatar small" :data-tone="account.tone">{{ account.initials }}</span>
          <div>
            <strong>{{ account.name }}</strong
            ><span class="auth-status" :data-status="account.status">{{
              account.statusLabel
            }}</span>
          </div>
          <button
            class="manage-mailboxes"
            :disabled="account.status !== 'ok'"
            @click="emit('manage', account.account)"
          >
            管理地址 · {{ account.aliases.length }} 个
          </button>
        </div>
        <p v-if="account.error" class="account-error">{{ account.error }}</p>
        <button
          v-for="alias in account.aliases"
          :key="alias.alias_id"
          class="account-address"
          :class="{
            selected: account.account === currentAccountId && alias.alias_id === currentAliasId,
          }"
          @click="emit('selectAddress', account.account, alias.alias_id)"
        >
          <span class="address-check"
            ><SvgIcon
              v-if="account.account === currentAccountId && alias.alias_id === currentAliasId"
              name="check"
          /></span>
          <span class="address-text">{{ alias.email }}</span>
          <span v-if="alias.is_primary" class="primary-tag">主地址</span>
        </button>
        <p v-if="account.aliases.length === 0" class="account-empty">
          {{ account.status === "ok" ? "暂无邮箱地址" : "该账号当前不可用" }}
        </p>
      </section>
      <p v-if="accounts.length === 0" class="account-empty">
        还没有邮箱账号，请在聊天中执行 /agently login 添加。
      </p>
    </div>
    <template #footer>
      <button class="button secondary" @click="emit('addAccount')">
        <SvgIcon name="plus" />添加账号
      </button>
      <span>账号授权请在聊天中执行 /agently login</span>
    </template>
  </DialogShell>
</template>
