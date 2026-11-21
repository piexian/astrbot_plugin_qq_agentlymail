<script setup lang="ts">
import { computed, shallowRef, watch } from "vue";
import { useI18n } from "../composables/useHostContext";
import type { Attachment, Message, Sender } from "../types/mail";
import { senderTone } from "../utils/display";
import { formatBytes, fullDateLabel } from "../utils/format";
import SvgIcon from "./SvgIcon.vue";
import MailBody from "./MailBody.vue";

const props = defineProps<{
  message: Message | null;
  address: string | null;
  loading: boolean;
  error: string | null;
  trashLoading: boolean;
  downloading: boolean;
  listCollapsed: boolean;
}>();
const emit = defineEmits<{
  back: [];
  toggleList: [];
  reply: [];
  forward: [];
  trash: [];
  download: [attachment: Attachment, index: number];
  retry: [];
}>();
const { t, locale } = useI18n();

const detailsOpen = shallowRef(false);
const attachmentsOpen = shallowRef(true);

watch(
  () => props.message?.message_id ?? null,
  () => {
    detailsOpen.value = false;
    attachmentsOpen.value = (props.message?.attachments.length ?? 0) <= 3;
  },
  { immediate: true },
);

function formatParticipants(list: Sender[] | undefined): string {
  if (!list || list.length === 0) return "";
  return list
    .map((item) =>
      item.name && item.name !== item.email ? `${item.name} <${item.email}>` : item.email,
    )
    .join(", ");
}

const senderName = computed(() => props.message?.from_?.name ?? props.message?.from_?.email ?? "");
const senderEmail = computed(() => props.message?.from_?.email ?? "");
const fromDisplay = computed(() => {
  const from = props.message?.from_;
  if (!from) return "";
  return from.name && from.name !== from.email ? `${from.name} <${from.email}>` : from.email;
});
const toList = computed(() => formatParticipants(props.message?.to) || props.address || "");
const ccList = computed(() => formatParticipants(props.message?.cc));
const bccList = computed(() => formatParticipants(props.message?.bcc));
const bodyFormatLabel = computed(() => (props.message?.body_format ?? "PLAIN").toUpperCase());
const tone = computed(() => senderTone(senderEmail.value));
const fullDate = computed(() =>
  props.message?.created_at ? fullDateLabel(props.message.created_at, locale.value) : "",
);
</script>

<template>
  <section class="reader-pane" :aria-label="t('reader.ariaLabel')">
    <div class="reader-toolbar">
      <div class="reader-toolbar-left">
        <button
          type="button"
          class="icon-button mobile-only"
          :aria-label="t('reader.back')"
          @click="emit('back')"
        >
          <SvgIcon name="back" />
        </button>
        <button
          type="button"
          class="reader-list-toggle desktop-only"
          :class="{ active: listCollapsed }"
          :aria-expanded="!listCollapsed"
          :aria-label="listCollapsed ? t('list.expand') : t('list.collapse')"
          :title="listCollapsed ? t('list.expand') : t('list.collapse')"
          @click="emit('toggleList')"
        >
          <SvgIcon name="panel-left" />
          <span>{{ listCollapsed ? t("list.expand") : t("list.collapse") }}</span>
        </button>
      </div>
      <div class="reader-actions">
        <button
          type="button"
          class="button secondary"
          :disabled="!message || loading"
          @click="emit('reply')"
        >
          <SvgIcon name="reply" />{{ t("reader.reply") }}
        </button>
        <button
          type="button"
          class="button secondary"
          :disabled="!message || loading"
          @click="emit('forward')"
        >
          <SvgIcon name="forward" />{{ t("reader.forward") }}
        </button>
        <button
          type="button"
          class="icon-button"
          :disabled="!message || trashLoading"
          :aria-label="t('reader.trash')"
          :title="t('reader.trash')"
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
        <button type="button" class="button secondary" @click="emit('retry')">
          {{ t("list.retry") }}
        </button>
      </div>
      <article v-else-if="message" class="mail-article" :class="{ wide: listCollapsed }">
        <h2 class="mail-subject">{{ message.subject }}</h2>
        <div class="sender-block">
          <span class="avatar" :data-tone="tone">{{ senderName.slice(0, 1) }}</span>
          <div class="sender-copy">
            <strong>{{ fromDisplay }}</strong>
            <span>{{ t("reader.sentTo", { address: address ?? "" }) }}</span>
          </div>
          <div class="sender-meta">
            <span class="sender-date">{{ fullDate }}</span>
            <button
              type="button"
              class="details-toggle"
              :class="{ active: detailsOpen }"
              :aria-expanded="detailsOpen"
              @click="detailsOpen = !detailsOpen"
            >
              <span>{{ detailsOpen ? t("reader.hideDetails") : t("reader.details") }}</span>
              <SvgIcon name="chevron" class="chevron-icon" :class="{ open: detailsOpen }" />
            </button>
          </div>
        </div>

        <dl v-if="detailsOpen" class="mail-meta-details">
          <div>
            <dt>{{ t("reader.from") }}</dt>
            <dd>{{ fromDisplay }}</dd>
          </div>
          <div v-if="toList">
            <dt>{{ t("reader.to") }}</dt>
            <dd>{{ toList }}</dd>
          </div>
          <div v-if="ccList">
            <dt>{{ t("reader.cc") }}</dt>
            <dd>{{ ccList }}</dd>
          </div>
          <div v-if="bccList">
            <dt>{{ t("reader.bcc") }}</dt>
            <dd>{{ bccList }}</dd>
          </div>
          <div v-if="fullDate">
            <dt>{{ t("reader.date") }}</dt>
            <dd>{{ fullDate }}</dd>
          </div>
          <div>
            <dt>{{ t("reader.format") }}</dt>
            <dd>{{ bodyFormatLabel }}</dd>
          </div>
        </dl>

        <section v-if="message.attachments.length" class="attachments-panel">
          <button
            type="button"
            class="attachments-toggle"
            :aria-expanded="attachmentsOpen"
            :aria-label="
              attachmentsOpen ? t('reader.collapseAttachments') : t('reader.expandAttachments')
            "
            @click="attachmentsOpen = !attachmentsOpen"
          >
            <SvgIcon name="paperclip" />
            <strong>{{
              t("reader.attachmentsHeading", { count: message.attachments.length })
            }}</strong>
            <SvgIcon name="chevron" class="chevron-icon" :class="{ open: attachmentsOpen }" />
          </button>
          <div v-if="attachmentsOpen" class="attachment-grid">
            <button
              v-for="(file, index) in message.attachments"
              :key="file.attachment_id ?? file.filename ?? index"
              type="button"
              class="attachment-card"
              :disabled="downloading"
              @click="emit('download', file, index)"
            >
              <SvgIcon name="paperclip" />
              <span class="attachment-copy">
                <strong>{{ file.filename }}</strong>
                <span>{{ formatBytes(file.size ?? 0) }}</span>
              </span>
            </button>
          </div>
        </section>

        <div class="mail-body">
          <MailBody :body="message.body" :body-format="message.body_format" />
        </div>
      </article>
      <div v-else class="reader-empty">
        <div class="empty-icon"><SvgIcon name="mail" /></div>
        <strong>{{ t("reader.emptyTitle") }}</strong>
        <p>{{ t("reader.emptyText") }}</p>
      </div>
    </div>
  </section>
</template>
