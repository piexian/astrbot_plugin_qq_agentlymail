<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "../composables/useHostContext";
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
const { t, locale } = useI18n();

const paragraphs = computed(() => (props.message ? props.message.body.split("\n\n") : []));
const senderName = computed(() => props.message?.from_?.name ?? props.message?.from_?.email ?? "");
const senderEmail = computed(() => props.message?.from_?.email ?? "");
const tone = computed(() => senderTone(senderEmail.value));
const fullDate = computed(() =>
  props.message?.created_at ? fullDateLabel(props.message.created_at, locale.value) : "",
);
const formatNote = computed(() => {
  const format = props.message?.body_format;
  if (!format || format === "PLAIN") return "";
  return t("reader.formatNote", { format });
});
</script>

<template>
  <section class="reader-pane" :aria-label="t('reader.ariaLabel')">
    <div class="reader-toolbar">
      <button class="icon-button mobile-only" :aria-label="t('reader.back')" @click="emit('back')">
        <SvgIcon name="back" />
      </button>
      <div class="reader-actions">
        <button class="button secondary" :disabled="!message || loading" @click="emit('reply')">
          <SvgIcon name="reply" />{{ t("reader.reply") }}
        </button>
        <button class="button secondary" :disabled="!message || loading" @click="emit('forward')">
          <SvgIcon name="forward" />{{ t("reader.forward") }}
        </button>
        <button
          class="icon-button"
          :disabled="!message || trashLoading"
          :aria-label="t('reader.trash')"
          @click="emit('trash')"
        >
          <SvgIcon name="trash" />
        </button>
      </div>
    </div>
    <div class="reader-content">
      <div v-if="loading" class="reader-empty">
        <div class="empty-icon"><SvgIcon name="refresh" /></div>
        <strong>{{ t("reader.loading") }}</strong>
      </div>
      <div v-else-if="error" class="reader-empty">
        <div class="empty-icon"><SvgIcon name="mail" /></div>
        <strong>{{ t("reader.loadFailed") }}</strong>
        <p>{{ error }}</p>
        <button class="button secondary" @click="emit('retry')">{{ t("list.retry") }}</button>
      </div>
      <article v-else-if="message" class="mail-article">
        <h2 class="mail-subject">{{ message.subject }}</h2>
        <div class="sender-block">
          <span class="avatar" :data-tone="tone">{{ senderName.slice(0, 1) }}</span>
          <div class="sender-copy">
            <strong>{{ senderName }} &lt;{{ senderEmail }}&gt;</strong>
            <span>{{ t("reader.sentTo", { address: address ?? "" }) }}</span>
          </div>
          <span class="sender-date">{{ fullDate }}</span>
        </div>
        <p v-if="formatNote" class="field-hint">{{ formatNote }}</p>
        <div class="mail-body">
          <p v-for="(paragraph, index) in paragraphs" :key="index">{{ paragraph }}</p>
        </div>
        <section v-if="message.attachments.length" class="attachments">
          <h3>{{ t("reader.attachmentsHeading", { count: message.attachments.length }) }}</h3>
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
        <strong>{{ t("reader.emptyTitle") }}</strong>
        <p>{{ t("reader.emptyText") }}</p>
      </div>
    </div>
  </section>
</template>
