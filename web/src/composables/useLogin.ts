/** WebUI 扫码登录流程：发起、轮询、刷新与随时中断；中断失败会显示错误并保持对话框打开。 */

import { computed, onScopeDispose, shallowRef } from "vue";
import type { MailApi } from "../api/mailApi";
import type { WebLoginSession } from "../types/mail";
import { errorMessage } from "../utils/error";
import { t } from "./useHostContext";
import type { Notify } from "./useToasts";

const POLL_INTERVAL_MS = 1200;
const ACTIVE_STATES = new Set(["waiting_scan"]);

export interface LoginDeps {
  api: MailApi;
  notify: Notify;
  onLoggedIn: (account: string) => Promise<void> | void;
}

export function useLogin(deps: LoginDeps) {
  const open = shallowRef(false);
  const accountInput = shallowRef("default");
  const session = shallowRef<WebLoginSession | null>(null);
  const starting = shallowRef(false);
  const cancelling = shallowRef(false);
  const localError = shallowRef<string | null>(null);

  let pollTimer: number | undefined;
  let flowToken = 0;

  const isInFlight = computed(
    () => session.value !== null && ACTIVE_STATES.has(session.value.state),
  );

  function stopPolling() {
    if (pollTimer !== undefined) {
      window.clearTimeout(pollTimer);
      pollTimer = undefined;
    }
  }

  function schedulePoll(sessionId: string, token: number) {
    stopPolling();
    pollTimer = window.setTimeout(() => {
      void pollOnce(sessionId, token);
    }, POLL_INTERVAL_MS);
  }

  async function pollOnce(sessionId: string, token: number) {
    if (token !== flowToken || !open.value) return;
    try {
      const next = await deps.api.loginStatus(sessionId);
      if (token !== flowToken) return;
      session.value = next;
      if (next.state === "success") {
        stopPolling();
        deps.notify(
          t("login.successToast", {
            account: next.account,
            email: next.email ?? next.account,
          }),
        );
        await deps.onLoggedIn(next.account);
        return;
      }
      if (ACTIVE_STATES.has(next.state)) {
        schedulePoll(sessionId, token);
      } else {
        stopPolling();
      }
    } catch (cause) {
      if (token !== flowToken) return;
      stopPolling();
      localError.value = errorMessage(cause);
    }
  }

  function openLogin(initialAccount = "default", autoStart = false) {
    stopPolling();
    flowToken += 1;
    accountInput.value = initialAccount.trim() || "default";
    session.value = null;
    localError.value = null;
    starting.value = false;
    cancelling.value = false;
    open.value = true;
    if (autoStart) {
      void startLogin();
    }
  }

  async function startLogin() {
    if (starting.value || cancelling.value) return;
    const account = accountInput.value.trim();
    if (!account) {
      localError.value = t("login.accountRequired");
      return;
    }
    stopPolling();
    const token = ++flowToken;
    starting.value = true;
    localError.value = null;
    try {
      const created = await deps.api.loginStart(account);
      if (token !== flowToken) {
        void deps.api.loginCancel(created.session_id).catch(() => {});
        return;
      }
      session.value = created;
      if (ACTIVE_STATES.has(created.state)) {
        schedulePoll(created.session_id, token);
      }
    } catch (cause) {
      if (token !== flowToken) return;
      localError.value = errorMessage(cause);
    } finally {
      if (token === flowToken) starting.value = false;
    }
  }

  async function refreshLink() {
    if (starting.value || cancelling.value) return;
    await startLogin();
  }
  /** 中断当前登录；失败时显示错误并保持会话与对话框，成功后才清空并提示。 */
  async function cancelLogin() {
    stopPolling();
    const activeId = session.value?.session_id;
    const shouldAbortBackend = Boolean(activeId && isInFlight.value);
    flowToken += 1;
    if (!shouldAbortBackend || !activeId) {
      session.value = null;
      localError.value = null;
      return;
    }
    cancelling.value = true;
    try {
      await deps.api.loginCancel(activeId);
    } catch (cause) {
      localError.value = errorMessage(cause);
      return;
    } finally {
      cancelling.value = false;
    }
    session.value = null;
    localError.value = null;
    deps.notify(t("login.cancelledToast"));
  }

  /** 关闭对话框；登录仍在途时先请求中断，中断失败则不关闭并显示错误。 */
  async function closeLogin() {
    stopPolling();
    const activeId = session.value?.session_id;
    if (!activeId || !isInFlight.value) {
      open.value = false;
      return;
    }
    flowToken += 1;
    cancelling.value = true;
    try {
      await deps.api.loginCancel(activeId);
    } catch (cause) {
      localError.value = errorMessage(cause);
      return;
    } finally {
      cancelling.value = false;
    }
    session.value = null;
    localError.value = null;
    open.value = false;
  }

  onScopeDispose(() => {
    stopPolling();
    if (session.value && isInFlight.value) {
      void deps.api.loginCancel(session.value.session_id).catch(() => {});
    }
  });

  return {
    open,
    accountInput,
    session,
    starting,
    cancelling,
    localError,
    isInFlight,
    openLogin,
    startLogin,
    refreshLink,
    cancelLogin,
    closeLogin,
  };
}
