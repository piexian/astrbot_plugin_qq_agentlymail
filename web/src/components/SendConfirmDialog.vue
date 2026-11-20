<script setup lang="ts">
import { computed } from "vue";
import type { PendingSend } from "../composables/useCompose";
import { useI18n } from "../composables/useHostContext";
import DialogShell from "./DialogShell.vue";

const props = defineProps<{
  open: boolean;
  pending: PendingSend | null;
  sending: boolean;
  error: string | null;
  resultUnknown: boolean;
  retryAcknowledged: boolean;
}>();
const emit = defineEmits<{
  close: [];
  confirm: [];
  acknowledgeRetry: [];
  checkSent: [];
}>();
const { t } = useI18n();

const attachmentLabel = computed(() => {
  if (!props.pending) return t("confirmSend.attachmentsNone");
  if (props.pending.mode === "forward")
    return props.pending.attachmentCount
      ? t("confirmSend.attachmentsForwardCount", { count: props.pending.attachmentCount })
      : t("confirmSend.attachmentsNone");
  if (props.pending.mode === "reply") return t("confirmSend.attachmentsNone");
  return props.pending.attachmentCount
    ? t("confirmSend.attachmentsCount", { count: props.pending.attachmentCount })
    : t("confirmSend.attachmentsNone");
});
const retryDisabled = computed(
  () => props.sending || (props.resultUnknown && !props.retryAcknowledged),
);
</script>

<template>
  <DialogShell
    :open="open"
    :title="t('confirmSend.title')"
    :subtitle="t('confirmSend.subtitle')"
    dialog-class="small-dialog"
    footer-end
    @close="emit('close')"
  >
    <div v-if="pending" class="dialog-body">
      <dl class="confirm-summary">
        <dt>{{ t("compose.from") }}</dt>
        <dd>{{ pending.from }}</dd>
        <dt>{{ t("compose.to") }}</dt>
        <dd>{{ pending.to }}</dd>
        <dt v-if="pending.cc">{{ t("compose.cc") }}</dt>
        <dd v-if="pending.cc">{{ pending.cc }}</dd>
        <dt>{{ t("compose.subject") }}</dt>
        <dd>{{ pending.subject }}</dd>
        <dt>{{ t("confirmSend.attachments") }}</dt>
        <dd>{{ attachmentLabel }}</dd>
      </dl>
      <div v-if="error" class="send-error" role="alert">
        <p>{{ error }}</p>
        <template v-if="resultUnknown">
          <p class="confirm-note">
            {{ t("confirmSend.resultUnknownNote") }}
          </p>
          <div class="send-error-actions">
            <button class="button secondary" @click="emit('checkSent')">
              {{ t("confirmSend.checkSent") }}
            </button>
            <button
              class="button secondary ack-button"
              :class="{ active: retryAcknowledged }"
              @click="emit('acknowledgeRetry')"
            >
              {{ retryAcknowledged ? t("confirmSend.ackedRetry") : t("confirmSend.ackRetry") }}
            </button>
          </div>
        </template>
      </div>
      <p v-else class="confirm-note">{{ t("confirmSend.confirmNote") }}</p>
    </div>
    <template #footer>
      <button class="button secondary" :disabled="sending" @click="emit('close')">
        {{ t("confirmSend.backToEdit") }}
      </button>
      <button class="button primary" :disabled="retryDisabled" @click="emit('confirm')">
        {{
          sending
            ? t("compose.sending")
            : error
              ? t("confirmSend.resend")
              : t("confirmSend.confirm")
        }}
      </button>
    </template>
  </DialogShell>
</template>
