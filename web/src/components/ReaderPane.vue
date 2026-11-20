<script setup lang="ts">
import { computed } from "vue";
import type { Attachment, Message } from "../types/mail";
import { senderTone } from "../utils/display";
import { formatBytes, fullDateLabel } from "../utils/format";
import SvgIcon from "./SvgIcon.vue";

const props = defineProps<{
  message: Message | null;
  address: string | null;
  loading: boolean;
  error: string | null;
  trashLoading: boolean;
  downloading: boolean;
}>();
const emit = defineEmits<{
  back: [];
  reply: [];
  forward: [];
  trash: [];
  download: [attachment: Attachment, index: number];
  retry: [];
}>();

const paragraphs = computed(() => (props.message ? props.message.body.split("\n\n") : []));
const senderName = computed(() => props.message?.from_?.name ?? props.message?.from_?.email ?? "");
const senderEmail = computed(() => props.message?.from_?.email ?? "");
const tone = computed(() => senderTone(senderEmail.value));
const fullDate = computed(() =>
  props.message?.created_at ? fullDateLabel(props.message.created_at) : "",
);
const formatNote = computed(() => {
  const format = props.message?.body_format;
  if (!format || format === "PLAIN") return "";
  return `正文格式为 ${format}，此处按纯文本显示。`;
});
</script>

<template>
  <section class="reader-pane" aria-label="邮件正文">
    <div class="reader-toolbar">
      <button class="icon-button mobile-only" aria-label="返回邮件列表" @click="emit('back')">
        <SvgIcon name="back" />
      </button>
      <div class="reader-actions">
        <button class="button secondary" :disabled="!message || loading" @click="emit('reply')">
          <SvgIcon name="reply" />回复
        </button>
        <button class="button secondary" :disabled="!message || loading" @click="emit('forward')">
          <SvgIcon name="forward" />转发
        </button>
        <button
          class="icon-button"
          :disabled="!message || trashLoading"
          aria-label="移到垃圾箱"
          @click="emit('trash')"
        >
          <SvgIcon name="trash" />
        </button>
      </div>
    </div>
    <div class="reader-content">
      <div v-if="loading" class="reader-empty">
        <div class="empty-icon"><SvgIcon name="refresh" /></div>
        <strong>正在加载正文…</strong>
      </div>
      <div v-else-if="error" class="reader-empty">
        <div class="empty-icon"><SvgIcon name="mail" /></div>
        <strong>正文加载失败</strong>
        <p>{{ error }}</p>
        <button class="button secondary" @click="emit('retry')">重试</button>
      </div>
      <article v-else-if="message" class="mail-article">
        <h2 class="mail-subject">{{ message.subject }}</h2>
        <div class="sender-block">
          <span class="avatar" :data-tone="tone">{{ senderName.slice(0, 1) }}</span>
          <div class="sender-copy">
            <strong>{{ senderName }} &lt;{{ senderEmail }}&gt;</strong>
            <span>发送至 {{ address }}</span>
          </div>
          <span class="sender-date">{{ fullDate }}</span>
        </div>
        <p v-if="formatNote" class="field-hint">{{ formatNote }}</p>
        <div class="mail-body">
          <p v-for="(paragraph, index) in paragraphs" :key="index">{{ paragraph }}</p>
        </div>
        <section v-if="message.attachments.length" class="attachments">
          <h3>附件 · {{ message.attachments.length }}</h3>
          <button
            v-for="(file, index) in message.attachments"
            :key="file.attachment_id ?? file.filename ?? index"
            class="attachment-card"
            :disabled="downloading"
            @click="emit('download', file, index)"
          >
            <SvgIcon name="paperclip" />
            <span class="attachment-copy"
              ><strong>{{ file.filename }}</strong
              ><span>{{ formatBytes(file.size ?? 0) }}</span></span
            >
          </button>
        </section>
      </article>
      <div v-else class="reader-empty">
        <div class="empty-icon"><SvgIcon name="mail" /></div>
        <strong>选择一封邮件</strong>
        <p>在这里阅读正文、回复或下载附件。</p>
      </div>
    </div>
  </section>
</template>
