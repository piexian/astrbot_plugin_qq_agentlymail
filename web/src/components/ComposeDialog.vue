<script setup lang="ts">
import { computed, nextTick, useTemplateRef, watch } from "vue";
import type { ComposeAttachment, ComposeMode } from "../composables/useCompose";
import { useI18n } from "../composables/useHostContext";
import type { Alias, Attachment, BodyFormat } from "../types/mail";
import { formatBytes } from "../utils/format";
import SvgIcon from "./SvgIcon.vue";

const props = defineProps<{
  open: boolean;
  mode: ComposeMode;
  aliases: Alias[];
  attachments: ComposeAttachment[];
  forwardAttachments: Attachment[];
  sending: boolean;
}>();
const emit = defineEmits<{
  close: [];
  addFiles: [files: File[]];
  removeAttachment: [key: string];
  saveDraft: [];
  submit: [];
}>();
const fromAliasId = defineModel<string>("fromAliasId", { required: true });
const to = defineModel<string>("to", { required: true });
const cc = defineModel<string>("cc", { required: true });
const subject = defineModel<string>("subject", { required: true });
const body = defineModel<string>("body", { required: true });
const format = defineModel<BodyFormat>("format", { required: true });

const dialogRef = useTemplateRef<HTMLDialogElement>("dialog");
const toInput = useTemplateRef<HTMLInputElement>("to");
const bodyInput = useTemplateRef<HTMLTextAreaElement>("body");
const fileInput = useTemplateRef<HTMLInputElement>("file");
const { t } = useI18n();

const title = computed(() =>
  props.mode === "reply"
    ? t("compose.titleReply")
    : props.mode === "forward"
      ? t("compose.titleForward")
      : t("compose.titleNew"),
);
const canAttach = computed(() => props.mode === "new");

watch(
  () => props.open,
  (value) => {
    const element = dialogRef.value;
    if (!element) return;
    if (value && !element.open) {
      element.showModal();
      void nextTick(() => {
        if (props.mode === "reply") bodyInput.value?.focus();
        else toInput.value?.focus();
      });
    } else if (!value && element.open) {
      element.close();
    }
  },
);

function onFilesChanged(event: Event) {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  input.value = "";
  if (files.length) emit("addFiles", files);
}
</script>

<template>
  <dialog
    ref="dialog"
    class="dialog compose-dialog"
    :aria-label="title"
    @cancel.prevent="emit('close')"
  >
    <form @submit.prevent="emit('submit')">
      <div class="compose-header">
        <h2>{{ title }}</h2>
        <button
          type="button"
          class="icon-button"
          :aria-label="t('compose.close')"
          @click="emit('close')"
        >
          <SvgIcon name="close" />
        </button>
      </div>
      <div class="compose-fields">
        <label
          ><span>{{ t("compose.from") }}</span
          ><select v-model="fromAliasId" required>
            <option v-for="alias in aliases" :key="alias.alias_id" :value="alias.alias_id">
              {{ alias.email }}
            </option>
          </select></label
        >
        <label
          ><span>{{ t("compose.to") }}</span
          ><input
            ref="to"
            v-model="to"
            type="email"
            required
            placeholder="name@example.com"
            autocomplete="off"
        /></label>
        <label
          ><span>{{ t("compose.cc") }}</span
          ><input
            v-model="cc"
            type="email"
            :placeholder="t('compose.ccPlaceholder')"
            autocomplete="off"
        /></label>
        <label
          ><span>{{ t("compose.subject") }}</span
          ><input
            v-model="subject"
            required
            maxlength="200"
            :placeholder="t('compose.subjectPlaceholder')"
        /></label>
      </div>
      <div class="compose-tools">
        <span>{{ t("compose.bodyLabel") }}</span>
        <select v-model="format" :aria-label="t('compose.bodyFormat')">
          <option value="PLAIN">{{ t("compose.formatPlain") }}</option>
          <option value="MARKDOWN">Markdown</option>
          <option value="HTML">HTML</option>
        </select>
        <button
          v-if="canAttach"
          type="button"
          class="icon-button"
          :aria-label="t('compose.addAttachment')"
          :disabled="sending"
          @click="fileInput?.click()"
        >
          <SvgIcon name="paperclip" />
        </button>
        <input v-if="canAttach" ref="file" type="file" multiple hidden @change="onFilesChanged" />
      </div>
      <textarea
        ref="body"
        v-model="body"
        class="compose-body"
        required
        :placeholder="t('compose.bodyPlaceholder')"
      ></textarea>
      <div v-if="mode === 'new'" class="attachment-list">
        <span v-for="file in attachments" :key="file.key" class="attachment-chip">
          <SvgIcon name="paperclip" /><span
            >{{ file.filename }} · {{ formatBytes(file.size) }}</span
          >
          <button
            type="button"
            :aria-label="t('compose.removeAttachment', { filename: file.filename })"
            :disabled="sending"
            @click="emit('removeAttachment', file.key)"
          >
            <SvgIcon name="close" />
          </button>
        </span>
      </div>
      <div v-else-if="mode === 'forward' && forwardAttachments.length" class="attachment-list">
        <span
          v-for="(file, index) in forwardAttachments"
          :key="file.attachment_id ?? index"
          class="attachment-chip readonly"
        >
          <SvgIcon name="paperclip" /><span
            >{{ file.filename }} · {{ formatBytes(file.size ?? 0) }}</span
          >
        </span>
      </div>
      <div class="compose-footer">
        <span v-if="mode === 'new'">{{ t("compose.footerNew") }}</span>
        <span v-else-if="mode === 'forward'">{{ t("compose.footerForward") }}</span>
        <span v-else>{{ t("compose.footerReply") }}</span>
        <div>
          <button
            v-if="mode === 'new'"
            type="button"
            class="button secondary"
            :disabled="sending"
            @click="emit('saveDraft')"
          >
            {{ t("compose.saveDraft") }}
          </button>
          <button class="button primary" type="submit" :disabled="sending">
            <SvgIcon name="send" />{{ sending ? t("compose.sending") : t("compose.send") }}
          </button>
        </div>
      </div>
    </form>
  </dialog>
</template>
