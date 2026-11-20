<script setup lang="ts">
import { useI18n } from "../composables/useHostContext";
import type { DisplayAccount } from "../types/mail";
import { accountStatusLabel } from "../utils/display";
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
  relogin: [accountId: string];
}>();
const { t } = useI18n();
</script>

<template>
  <DialogShell
    :open="open"
    :title="t('accountDialog.title')"
    :subtitle="t('accountDialog.subtitle')"
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
              accountStatusLabel(account.status)
            }}</span>
          </div>
          <div class="account-actions">
            <button
              v-if="account.status !== 'ok'"
              class="manage-mailboxes"
              @click="emit('relogin', account.account)"
            >
              {{ t("accountDialog.relogin") }}
            </button>
            <button
              class="manage-mailboxes"
              :disabled="account.status !== 'ok'"
              @click="emit('manage', account.account)"
            >
              {{ t("accountDialog.manageCount", { count: account.aliases.length }) }}
            </button>
          </div>
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
          <span v-if="alias.is_primary" class="primary-tag">{{
            t("accountDialog.primaryTag")
          }}</span>
        </button>
        <p v-if="account.aliases.length === 0" class="account-empty">
          {{ account.status === "ok" ? t("sidebar.noAddress") : t("accountDialog.unavailable") }}
        </p>
      </section>
      <p v-if="accounts.length === 0" class="account-empty">
        {{ t("accountDialog.empty") }}
      </p>
    </div>
    <template #footer>
      <button class="button secondary" @click="emit('addAccount')">
        <SvgIcon name="plus" />{{ t("accountDialog.addAccount") }}
      </button>
      <span>{{ t("accountDialog.footerHint") }}</span>
    </template>
  </DialogShell>
</template>
