<script setup lang="ts">
import { useI18n } from "../composables/useHostContext";
import type { Alias } from "../types/mail";
import DialogShell from "./DialogShell.vue";

defineProps<{ open: boolean; alias: Alias | null; accountName: string; loading: boolean }>();
const emit = defineEmits<{ close: []; confirm: [] }>();
const { t } = useI18n();
</script>

<template>
  <DialogShell
    :open="open"
    :title="t('deleteAliasDialog.title')"
    :subtitle="t('deleteAliasDialog.subtitle', { account: accountName })"
    dialog-class="small-dialog"
    footer-end
    @close="emit('close')"
  >
    <div class="dialog-body">
      <strong>{{ alias?.email ?? alias?.alias_id }}</strong>
      <ul class="risk-list">
        <li>{{ t("deleteAliasDialog.riskDeleteMails") }}</li>
        <li>{{ t("deleteAliasDialog.riskUnbindAgent") }}</li>
        <li>{{ t("deleteAliasDialog.riskRetention") }}</li>
      </ul>
      <p class="confirm-note">{{ t("deleteAliasDialog.confirmNote") }}</p>
    </div>
    <template #footer>
      <button class="button secondary" :disabled="loading" @click="emit('close')">
        {{ t("deleteAliasDialog.cancel") }}
      </button>
      <button class="button danger" :disabled="loading" @click="emit('confirm')">
        {{ loading ? t("deleteAliasDialog.deleting") : t("deleteAliasDialog.confirm") }}
      </button>
    </template>
  </DialogShell>
</template>
