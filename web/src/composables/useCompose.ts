/** 写邮件工作流：真实 File 附件、确认时转 Base64、send/reply/forward 与写失败降级。 */

import { computed, reactive, shallowRef } from "vue";
import {
  ATTACHMENT_MAX_BYTES,
  ATTACHMENT_MAX_COUNT,
  type MailApi,
  type MessageRef,
} from "../api/mailApi";
import type {
  Attachment,
  BodyFormat,
  DisplayAccount,
  Message,
  OutboundAttachment,
} from "../types/mail";
import { errorMessage } from "../utils/error";
import { t } from "./useHostContext";
import type { Notify } from "./useToasts";

export type ComposeMode = "new" | "reply" | "forward";

export interface ComposeAttachment {
  key: string;
  filename: string;
  size: number;
  file: File;
}

export interface PendingSend {
  mode: ComposeMode;
  from: string;
  to: string;
  cc: string;
  subject: string;
  attachmentCount: number;
}

interface Draft {
  aliasId: string;
  to: string;
  cc: string;
  subject: string;
  body: string;
  format: BodyFormat;
  attachments: ComposeAttachment[];
}

export interface ComposeDeps {
  api: MailApi;
  notify: Notify;
  getAccount: () => DisplayAccount | null;
  getCurrentAlias: () => { alias_id: string } | null;
  getSelectedMessage: () => Message | null;
  onSent: () => void;
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        reject(new Error(t("compose.attachmentReadError")));
        return;
      }
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.onerror = () => reject(new Error(t("compose.attachmentReadError")));
    reader.readAsDataURL(file);
  });
}

export function useCompose(deps: ComposeDeps) {
  const open = shallowRef(false);
  const confirmOpen = shallowRef(false);
  const mode = shallowRef<ComposeMode>("new");
  const form = reactive({
    fromAliasId: "",
    to: "",
    cc: "",
    subject: "",
    body: "",
    format: "PLAIN" as BodyFormat,
  });
  const attachments = shallowRef<ComposeAttachment[]>([]);
  const forwardAttachments = shallowRef<Attachment[]>([]);
  const pendingSend = shallowRef<PendingSend | null>(null);
  const sending = shallowRef(false);
  const sendError = shallowRef<string | null>(null);
  const retryAcknowledged = shallowRef(false);
  const drafts = reactive<Record<string, Draft>>({});
  const replyForwardRef = shallowRef<MessageRef | null>(null);
  let attachmentSeq = 0;

  // 后端写超时文案含“结果未知”；此时须先检查已发送，用户明确确认后才允许再次提交。
  const resultUnknown = computed(
    () =>
      (sendError.value?.includes("结果未知") ||
        sendError.value?.toLowerCase().includes("unknown")) ??
      false,
  );

  function draftKey(aliasId: string) {
    return `${deps.getAccount()?.account ?? ""}:${aliasId}`;
  }

  function openCompose(nextMode: ComposeMode) {
    const account = deps.getAccount();
    if (!account) return;
    const message = deps.getSelectedMessage();
    if (nextMode !== "new" && !message) {
      deps.notify(t("compose.selectMessageFirst"));
      return;
    }
    if (nextMode === "new" && account.aliases.length === 0) {
      deps.notify(t("compose.noAliasToCompose"));
      return;
    }
    mode.value = nextMode;
    form.fromAliasId = deps.getCurrentAlias()?.alias_id ?? account.aliases[0]?.alias_id ?? "";
    form.to = "";
    form.cc = "";
    form.subject = "";
    form.body = "";
    form.format = "PLAIN";
    attachments.value = [];
    forwardAttachments.value = [];
    pendingSend.value = null;
    sendError.value = null;
    retryAcknowledged.value = false;
    replyForwardRef.value = null;

    if (nextMode === "reply" && message) {
      replyForwardRef.value = {
        account: account.account,
        alias_id: form.fromAliasId,
        message_id: message.message_id,
      };
      form.to = message.from_?.email ?? "";
      const subject = message.subject ?? "";
      form.subject = /^Re:/i.test(subject) ? subject : `Re: ${subject}`;
      form.body = `\n\n${t("compose.replyDivider")}\n${message.from_?.name ?? message.from_?.email ?? ""}：\n${message.body}`;
    } else if (nextMode === "forward" && message) {
      replyForwardRef.value = {
        account: account.account,
        alias_id: form.fromAliasId,
        message_id: message.message_id,
      };
      const subject = message.subject ?? "";
      form.subject = /^Fwd:/i.test(subject) ? subject : `Fwd: ${subject}`;
      form.body = `\n\n${t("compose.forwardDivider")}\n${t("compose.forwardSenderPrefix")}${message.from_?.email ?? ""}\n\n${message.body}`;
      forwardAttachments.value = message.attachments.map((file) => ({ ...file }));
    } else {
      const saved = drafts[draftKey(form.fromAliasId)];
      if (saved) {
        form.fromAliasId = saved.aliasId;
        form.to = saved.to;
        form.cc = saved.cc;
        form.subject = saved.subject;
        form.body = saved.body;
        form.format = saved.format;
        attachments.value = saved.attachments.map((file) => ({ ...file }));
      }
    }
    open.value = true;
  }

  function close() {
    open.value = false;
  }

  function addFiles(files: File[]) {
    if (mode.value !== "new") return;
    const incoming = files.map((file) => ({
      key: `att-${++attachmentSeq}`,
      filename: file.name,
      size: file.size,
      file,
    }));
    const combined = [...attachments.value, ...incoming];
    if (combined.length > ATTACHMENT_MAX_COUNT) {
      deps.notify(t("compose.maxAttachments", { max: ATTACHMENT_MAX_COUNT }));
      return;
    }
    if (combined.some((file) => file.size > ATTACHMENT_MAX_BYTES)) {
      deps.notify(t("compose.maxSingleAttachment"));
      return;
    }
    if (combined.reduce((sum, file) => sum + file.size, 0) > ATTACHMENT_MAX_BYTES) {
      deps.notify(t("compose.maxTotalAttachments"));
      return;
    }
    attachments.value = combined;
  }

  function removeAttachment(key: string) {
    attachments.value = attachments.value.filter((file) => file.key !== key);
  }

  function saveDraft() {
    if (mode.value !== "new") return;
    drafts[draftKey(form.fromAliasId)] = {
      aliasId: form.fromAliasId,
      to: form.to,
      cc: form.cc,
      subject: form.subject,
      body: form.body,
      format: form.format,
      attachments: attachments.value.map((file) => ({ ...file })),
    };
    open.value = false;
    deps.notify(t("compose.draftSaved"));
  }

  function submit() {
    if (sending.value) return;
    const account = deps.getAccount();
    const alias = account?.aliases.find((item) => item.alias_id === form.fromAliasId);
    const to = form.to.trim();
    const subject = form.subject.trim();
    const body = form.body.trim();
    if (!alias || !to || !subject || !body) {
      deps.notify(t("compose.missingRequired"));
      return;
    }
    if (mode.value !== "new" && !replyForwardRef.value) {
      deps.notify(t("compose.missingOriginal"));
      return;
    }
    const attachmentCount =
      mode.value === "new"
        ? attachments.value.length
        : mode.value === "forward"
          ? forwardAttachments.value.length
          : 0;
    pendingSend.value = {
      mode: mode.value,
      from: alias.email ?? alias.alias_id,
      to,
      cc: form.cc.trim(),
      subject,
      attachmentCount,
    };
    sendError.value = null;
    retryAcknowledged.value = false;
    confirmOpen.value = true;
  }

  async function confirmSend() {
    const pending = pendingSend.value;
    if (!pending || sending.value) return;
    if (resultUnknown.value && !retryAcknowledged.value) return;
    const account = deps.getAccount();
    if (!account) return;
    sending.value = true;
    sendError.value = null;
    try {
      let localRecorded = true;
      if (mode.value === "new") {
        const alias = account.aliases.find((item) => item.alias_id === form.fromAliasId);
        if (!alias) throw new Error(t("compose.fromMissing"));
        const outbound: OutboundAttachment[] = await Promise.all(
          attachments.value.map(async (file) => ({
            filename: file.filename,
            content_type: file.file.type || undefined,
            content: await fileToBase64(file.file),
          })),
        );
        const result = await deps.api.send({
          account: account.account,
          alias_id: alias.alias_id,
          to: [pending.to],
          cc: pending.cc ? [pending.cc] : [],
          subject: pending.subject,
          body: form.body.trim(),
          body_format: form.format,
          attachments: outbound,
        });
        localRecorded = result.local_count_recorded;
        delete drafts[draftKey(alias.alias_id)];
      } else if (mode.value === "reply") {
        const ref = replyForwardRef.value as MessageRef;
        const result = await deps.api.reply({
          ...ref,
          body: form.body.trim(),
          body_format: form.format,
          reply_all: false,
        });
        localRecorded = result.local_count_recorded;
      } else {
        const ref = replyForwardRef.value as MessageRef;
        const result = await deps.api.forward({
          ...ref,
          to: [pending.to],
          body: form.body.trim(),
          body_format: form.format,
          include_attachments: true,
        });
        localRecorded = result.local_count_recorded;
      }
      pendingSend.value = null;
      confirmOpen.value = false;
      open.value = false;
      deps.notify(t("compose.sentToast"));
      if (!localRecorded) deps.notify(t("compose.localCountFailed"));
      deps.onSent();
    } catch (cause) {
      // 写失败不自动重试；保留草稿与确认弹层，结果未知时引导先检查已发送。
      sendError.value = errorMessage(cause);
      retryAcknowledged.value = false;
    } finally {
      sending.value = false;
    }
  }

  function acknowledgeRetry() {
    retryAcknowledged.value = true;
  }

  function cancelConfirm() {
    confirmOpen.value = false;
    sendError.value = null;
    retryAcknowledged.value = false;
  }

  return {
    open,
    confirmOpen,
    mode,
    form,
    attachments,
    forwardAttachments,
    pendingSend,
    sending,
    sendError,
    resultUnknown,
    retryAcknowledged,
    openCompose,
    close,
    addFiles,
    removeAttachment,
    saveDraft,
    submit,
    confirmSend,
    acknowledgeRetry,
    cancelConfirm,
  };
}

export type Compose = ReturnType<typeof useCompose>;
