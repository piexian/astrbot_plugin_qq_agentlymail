<script setup lang="ts">
import { computed, onUnmounted, shallowRef, watch } from "vue";
import { useI18n } from "../composables/useHostContext";
import type { WebLoginSession } from "../types/mail";
import { copyText } from "../utils/clipboard";
import DialogShell from "./DialogShell.vue";
import SvgIcon from "./SvgIcon.vue";

const props = defineProps<{
  open: boolean;
  session: WebLoginSession | null;
  starting: boolean;
  cancelling: boolean;
  localError: string | null;
  isInFlight: boolean;
}>();

const emit = defineEmits<{
  close: [];
  start: [];
  refresh: [];
  abort: [];
  copied: [message: string];
}>();
const account = defineModel<string>("account", { required: true });
const { t } = useI18n();

const copiedUrl = shallowRef(false);
let copiedTimer: number | undefined;

watch(
  () => props.open,
  (isOpen) => {
    if (!isOpen) {
      if (copiedTimer !== undefined) {
        window.clearTimeout(copiedTimer);
        copiedTimer = undefined;
      }
      copiedUrl.value = false;
    }
  },
);

onUnmounted(() => {
  if (copiedTimer !== undefined) {
    window.clearTimeout(copiedTimer);
    copiedTimer = undefined;
  }
});

const statusBanner = computed(() => {
  const s = props.session;
  if (!s) return null;
  if (s.state === "success") return { tone: "success", text: t("login.statusSuccess") };
  if (s.state === "expired") return { tone: "danger", text: t("login.statusExpired") };
  if (s.state === "error") return { tone: "danger", text: t("login.statusError") };
  return { tone: "info", text: t("login.statusWaiting") };
});

const displayError = computed(() => {
  if (props.localError) return props.localError;
  const s = props.session;
  if (!s || (s.state !== "error" && s.state !== "expired")) return null;
  switch (s.error_code) {
    case "expired":
      return t("login.errExpired");
    case "access_denied":
      return t("login.errAccessDenied");
    default:
      return s.error || t("login.statusError");
  }
});
async function copyUrl(value: string) {
  const ok = await copyText(value);
  if (ok) {
    copiedUrl.value = true;
    if (copiedTimer !== undefined) window.clearTimeout(copiedTimer);
    copiedTimer = window.setTimeout(() => {
      copiedUrl.value = false;
    }, 2000);
    emit("copied", t("login.copiedUrlToast"));
  } else {
    emit("copied", t("login.copyFailedToast"));
  }
}
</script>

<template>
  <DialogShell
    :open="open"
    :title="t('login.title')"
    :subtitle="t('login.subtitle')"
    dialog-class="login-dialog"
    @close="emit('close')"
  >
    <div class="dialog-body login-body">
      <form v-if="!session" class="login-start-form" @submit.prevent="emit('start')">
        <label class="login-field">
          <span>{{ t("login.accountLabel") }}</span>
          <input
            v-model="account"
            type="text"
            maxlength="64"
            required
            autocomplete="off"
            :disabled="starting"
            :placeholder="t('login.accountPlaceholder')"
          />
        </label>
        <p class="field-hint">{{ t("login.accountHint") }}</p>
        <p v-if="displayError" class="login-error-box" role="alert">{{ displayError }}</p>
      </form>

      <template v-else>
        <div class="login-status-row">
          <span class="login-account-pill">{{ session.account }}</span>
          <span v-if="statusBanner" class="login-status-pill" :data-tone="statusBanner.tone">
            {{ statusBanner.text }}
          </span>
        </div>

        <div v-if="session.state === 'success'" class="login-success-card">
          <div class="login-success-icon"><SvgIcon name="check" /></div>
          <strong>{{ t("login.statusSuccess") }}</strong>
          <p>
            {{
              session.email
                ? t("login.successDetail", { account: session.account, email: session.email })
                : t("login.successNoEmail", { account: session.account })
            }}
          </p>
        </div>

        <template v-else>
          <p v-if="displayError" class="login-error-box" role="alert">{{ displayError }}</p>

          <div v-if="session.browser_url" class="login-fallback-box">
            <div class="login-copy-block">
              <span class="login-copy-label">{{ t("login.urlLabel") }}</span>
              <div class="login-copy-row">
                <input
                  type="text"
                  class="login-readonly-input"
                  :value="session.browser_url"
                  readonly
                  @focus="($event.target as HTMLInputElement).select()"
                />
                <button
                  type="button"
                  class="button secondary login-copy-btn"
                  @click="copyUrl(session.browser_url)"
                >
                  <SvgIcon :name="copiedUrl ? 'check' : 'copy'" />
                  <span>{{ copiedUrl ? t("login.copied") : t("login.copyUrl") }}</span>
                </button>
              </div>
            </div>
          </div>
        </template>
      </template>
    </div>

    <template #footer>
      <template v-if="!session">
        <button type="button" class="button secondary" :disabled="starting" @click="emit('close')">
          {{ t("deleteAliasDialog.cancel") }}
        </button>
        <button type="button" class="button primary" :disabled="starting" @click="emit('start')">
          {{ starting ? t("login.starting") : t("login.startButton") }}
        </button>
      </template>

      <template v-else-if="session.state === 'success'">
        <span></span>
        <button type="button" class="button primary" @click="emit('close')">
          {{ t("login.done") }}
        </button>
      </template>

      <template v-else-if="isInFlight">
        <button
          type="button"
          class="button secondary"
          :disabled="cancelling || starting"
          @click="emit('abort')"
        >
          {{ cancelling ? t("login.aborting") : t("login.abort") }}
        </button>
        <button
          type="button"
          class="button primary"
          :disabled="starting || cancelling"
          @click="emit('refresh')"
        >
          <SvgIcon name="refresh" />{{ starting ? t("login.refreshing") : t("login.refreshLink") }}
        </button>
      </template>

      <template v-else>
        <button type="button" class="button secondary" :disabled="starting" @click="emit('close')">
          {{ t("dialog.close") }}
        </button>
        <button type="button" class="button primary" :disabled="starting" @click="emit('refresh')">
          <SvgIcon name="refresh" />{{ starting ? t("login.refreshing") : t("login.retry") }}
        </button>
      </template>
    </template>
  </DialogShell>
</template>
