<script setup lang="ts">
import { computed, nextTick, useTemplateRef, watch } from "vue";
import type { ComposeAttachment, ComposeMode } from "../composables/useCompose";
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

const title = computed(() =>
  props.mode === "reply" ? "回复邮件" : props.mode === "forward" ? "转发邮件" : "新邮件",
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
        <button type="button" class="icon-button" aria-label="关闭写邮件" @click="emit('close')">
          <SvgIcon name="close" />
        </button>
      </div>
      <div class="compose-fields">
        <label
          ><span>发件人</span
          ><select v-model="fromAliasId" required>
            <option v-for="alias in aliases" :key="alias.alias_id" :value="alias.alias_id">
              {{ alias.email }}
            </option>
          </select></label
        >
        <label
          ><span>收件人</span
          ><input
            ref="to"
            v-model="to"
            type="email"
            required
            placeholder="name@example.com"
            autocomplete="off"
        /></label>
        <label
          ><span>抄送</span><input v-model="cc" type="email" placeholder="可选" autocomplete="off"
        /></label>
        <label
          ><span>主题</span
          ><input v-model="subject" required maxlength="200" placeholder="邮件主题"
        /></label>
      </div>
      <div class="compose-tools">
        <span>正文</span>
        <select v-model="format" aria-label="正文格式">
          <option value="PLAIN">纯文本</option>
          <option value="MARKDOWN">Markdown</option>
          <option value="HTML">HTML</option>
        </select>
        <button
          v-if="canAttach"
          type="button"
          class="icon-button"
          aria-label="添加附件"
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
        placeholder="写下邮件内容……"
      ></textarea>
      <div v-if="mode === 'new'" class="attachment-list">
        <span v-for="file in attachments" :key="file.key" class="attachment-chip">
          <SvgIcon name="paperclip" /><span
            >{{ file.filename }} · {{ formatBytes(file.size) }}</span
          >
          <button
            type="button"
            :aria-label="`移除附件 ${file.filename}`"
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
        <span v-if="mode === 'new'">单附件 20 MB，总计 20 MB</span>
        <span v-else-if="mode === 'forward'">原附件由服务端携带</span>
        <span v-else>回复不携带附件</span>
        <div>
          <button
            v-if="mode === 'new'"
            type="button"
            class="button secondary"
            :disabled="sending"
            @click="emit('saveDraft')"
          >
            保存草稿
          </button>
          <button class="button primary" type="submit" :disabled="sending">
            <SvgIcon name="send" />{{ sending ? "发送中…" : "发送邮件" }}
          </button>
        </div>
      </div>
    </form>
  </dialog>
</template>
