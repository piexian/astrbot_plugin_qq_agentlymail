/** WebUI 微信扫码登录流程：发起、轮询、刷新、多邮箱选择与随时中断。 */

import { computed, onScopeDispose, shallowRef } from "vue";
import type { MailApi } from "../api/mailApi";
import type { WebLoginSession } from "../types/mail";
import { errorMessage } from "../utils/error";
import { t } from "./useHostContext";
import type { Notify } from "./useToasts";

const POLL_INTERVAL_MS = 1200;
const ACTIVE_STATES = new Set(["waiting_scan", "scanned", "select_mail", "authorizing"]);

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
  const selecting = shallowRef(false);
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
    selecting.value = false;
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

  async function refreshQr() {
    if (starting.value || cancelling.value) return;
    await startLogin();
  }

  async function selectMail(email: string) {
    const current = session.value;
    if (!current || current.state !== "select_mail" || selecting.value) return;
    const token = flowToken;
    selecting.value = true;
    localError.value = null;
    try {
      const updated = await deps.api.loginSelect(current.session_id, email);
      if (token !== flowToken) return;
      session.value = updated;
      if (ACTIVE_STATES.has(updated.state)) {
        schedulePoll(updated.session_id, token);
      }
    } catch (cause) {
      if (token !== flowToken) return;
      localError.value = errorMessage(cause);
    } finally {
      if (token === flowToken) selecting.value = false;
    }
  }

  async function cancelLogin(keepOpen = true) {
    stopPolling();
    const activeId = session.value?.session_id;
    const shouldAbortBackend = Boolean(activeId && isInFlight.value);
    flowToken += 1;
    if (shouldAbortBackend && activeId) {
      cancelling.value = true;
      try {
        await deps.api.loginCancel(activeId);
      } catch {
        // 中断失败不阻塞关闭对话框。
      } finally {
        cancelling.value = false;
      }
      if (keepOpen) deps.notify(t("login.cancelledToast"));
    }
    session.value = null;
    localError.value = null;
    if (!keepOpen) {
      open.value = false;
    }
  }

  async function closeLogin() {
    await cancelLogin(false);
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
    selecting,
    cancelling,
    localError,
    isInFlight,
    openLogin,
    startLogin,
    refreshQr,
    selectMail,
    cancelLogin,
    closeLogin,
  };
}
