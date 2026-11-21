<script setup lang="ts">
/** 移入垃圾箱前的本地确认：确认前零 API 请求，确认后才由父级调用 trash。 */
import { useI18n } from "../composables/useHostContext";
import DialogShell from "./DialogShell.vue";

defineProps<{
  open: boolean;
  subject: string;
  loading: boolean;
  error: string | null;
}>();
const emit = defineEmits<{
  close: [];
  confirm: [];
}>();
const { t } = useI18n();
</script>

<template>
  <DialogShell
    :open="open"
    :title="t('trashConfirm.title')"
    :subtitle="t('trashConfirm.subtitle')"
    dialog-class="small-dialog"
    footer-end
    @close="emit('close')"
  >
    <div class="dialog-body">
      <dl class="confirm-summary">
        <dt>{{ t("trashConfirm.subject") }}</dt>
        <dd>{{ subject }}</dd>
      </dl>
      <div v-if="error" class="send-error" role="alert">
        <p>{{ error }}</p>
      </div>
      <p v-else class="confirm-note">{{ t("trashConfirm.note") }}</p>
    </div>
    <template #footer>
      <button class="button secondary" :disabled="loading" @click="emit('close')">
        {{ t("trashConfirm.cancel") }}
      </button>
      <button class="button danger" :disabled="loading" @click="emit('confirm')">
        {{ loading ? t("trashConfirm.confirming") : t("trashConfirm.confirm") }}
      </button>
    </template>
  </DialogShell>
</template>
