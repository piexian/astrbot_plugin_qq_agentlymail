<script setup lang="ts">
import { computed } from "vue";
import type { PendingSend } from "../composables/useCompose";
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

const attachmentLabel = computed(() => {
  if (!props.pending) return "无";
  if (props.pending.mode === "forward")
    return props.pending.attachmentCount
      ? `${props.pending.attachmentCount} 个（服务端携带）`
      : "无";
  if (props.pending.mode === "reply") return "无";
  return props.pending.attachmentCount ? `${props.pending.attachmentCount} 个` : "无";
});
const retryDisabled = computed(
  () => props.sending || (props.resultUnknown && !props.retryAcknowledged),
);
</script>

<template>
  <DialogShell
    :open="open"
    title="确认发送邮件"
    subtitle="确认后将通过真实服务发送"
    dialog-class="small-dialog"
    footer-end
    @close="emit('close')"
  >
    <div v-if="pending" class="dialog-body">
      <dl class="confirm-summary">
        <dt>发件人</dt>
        <dd>{{ pending.from }}</dd>
        <dt>收件人</dt>
        <dd>{{ pending.to }}</dd>
        <dt v-if="pending.cc">抄送</dt>
        <dd v-if="pending.cc">{{ pending.cc }}</dd>
        <dt>主题</dt>
        <dd>{{ pending.subject }}</dd>
        <dt>附件</dt>
        <dd>{{ attachmentLabel }}</dd>
      </dl>
      <div v-if="error" class="send-error" role="alert">
        <p>{{ error }}</p>
        <template v-if="resultUnknown">
          <p class="confirm-note">
            发送结果未知。请先检查已发送文件夹，确认邮件未送达后再决定是否重新发送，避免重复投递。
          </p>
          <div class="send-error-actions">
            <button class="button secondary" @click="emit('checkSent')">查看已发送</button>
            <button
              class="button secondary ack-button"
              :class="{ active: retryAcknowledged }"
              @click="emit('acknowledgeRetry')"
            >
              {{ retryAcknowledged ? "已确认需要重新发送" : "我已检查已发送，确认需要重新发送" }}
            </button>
          </div>
        </template>
      </div>
      <p v-else class="confirm-note">确认后邮件将真实发送，请核对收件人与内容。</p>
    </div>
    <template #footer>
      <button class="button secondary" :disabled="sending" @click="emit('close')">返回修改</button>
      <button class="button primary" :disabled="retryDisabled" @click="emit('confirm')">
        {{ sending ? "发送中…" : error ? "重新发送" : "确认发送" }}
      </button>
    </template>
  </DialogShell>
</template>
