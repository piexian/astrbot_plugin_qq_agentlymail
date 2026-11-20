/** 选中后拉取邮件全文；令牌守卫丢弃快速切换产生的旧响应。 */

import { shallowRef, watch } from "vue";
import type { MailApi } from "../api/mailApi";
import type { Message } from "../types/mail";
import { errorMessage } from "../utils/error";

export interface ReaderDeps {
  api: MailApi;
  account: () => string | null;
  alias: () => string | null;
  messageId: () => string | null;
}

export function useReader(deps: ReaderDeps) {
  const message = shallowRef<Message | null>(null);
  const loading = shallowRef(false);
  const error = shallowRef<string | null>(null);

  let token = 0;

  async function load() {
    const account = deps.account();
    const alias = deps.alias();
    const messageId = deps.messageId();
    const current = ++token;
    if (!account || !alias || !messageId) {
      message.value = null;
      loading.value = false;
      error.value = null;
      return;
    }
    loading.value = true;
    error.value = null;
    message.value = null;
    try {
      const result = await deps.api.message({ account, alias_id: alias, message_id: messageId });
      if (current !== token) return;
      message.value = result;
    } catch (cause) {
      if (current !== token) return;
      error.value = errorMessage(cause);
    } finally {
      if (current === token) loading.value = false;
    }
  }

  watch(
    [() => deps.account(), () => deps.alias(), () => deps.messageId()],
    () => {
      void load();
    },
    { immediate: true },
  );

  function retry() {
    void load();
  }

  return { message, loading, error, retry };
}

export type Reader = ReturnType<typeof useReader>;
