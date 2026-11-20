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
  selecting: boolean;
  cancelling: boolean;
  localError: string | null;
  isInFlight: boolean;
}>();

const emit = defineEmits<{
  close: [];
  start: [];
  refresh: [];
  selectMail: [email: string];
  abort: [];
  copied: [message: string];
}>();

const account = defineModel<string>("account", { required: true });
const { t } = useI18n();

const copiedKind = shallowRef<"url" | "code" | null>(null);
let copiedTimer: number | undefined;

watch(
  () => props.open,
  (isOpen) => {
    if (!isOpen) {
      if (copiedTimer !== undefined) {
        window.clearTimeout(copiedTimer);
        copiedTimer = undefined;
      }
      copiedKind.value = null;
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
  if (s.state === "scanned") return { tone: "info", text: t("login.statusScanned") };
  if (s.state === "select_mail") return { tone: "info", text: t("login.statusSelectMail") };
  if (s.state === "authorizing") return { tone: "info", text: t("login.statusAuthorizing") };
  if (s.state === "expired") return { tone: "danger", text: t("login.statusExpired") };
  if (s.state === "error") return { tone: "danger", text: t("login.statusError") };
  if (s.qr_expired) return { tone: "warn", text: t("login.statusQrExpired") };
  if (s.mode === "browser" || !s.qr_image)
    return { tone: "info", text: t("login.statusBrowserOnly") };
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
    case "wx_cancelled":
      return t("login.errWxCancelled");
    case "wx_601":
      return t("login.errWx601");
    case "wx_75111":
      return t("login.errWx75111");
    case "wx_5012":
      return t("login.errWx5012");
    case "transport_error":
      return t("login.errTransport");
    default:
      return s.error || t("login.statusError");
  }
});

async function copyField(kind: "url" | "code", value: string) {
  const ok = await copyText(value);
  if (ok) {
    copiedKind.value = kind;
    if (copiedTimer !== undefined) window.clearTimeout(copiedTimer);
    copiedTimer = window.setTimeout(() => {
      copiedKind.value = null;
    }, 2000);
    emit("copied", kind === "url" ? t("login.copiedUrlToast") : t("login.copiedCodeToast"));
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

        <div v-else-if="session.state === 'select_mail'" class="login-mail-picker">
          <button
            v-for="item in session.mails ?? []"
            :key="item.email"
            type="button"
            class="account-address"
            :disabled="selecting"
            @click="emit('selectMail', item.email)"
          >
            <span class="address-text">{{ item.email }}</span>
            <span v-if="item.is_selected" class="primary-tag">{{ t("login.defaultMailTag") }}</span>
          </button>
        </div>

        <template v-else>
          <div v-if="session.qr_image" class="login-qr-stage">
            <div class="login-qr-frame" :class="{ dimmed: session.state === 'scanned' }">
              <img :src="session.qr_image" :alt="t('login.qrAlt')" class="login-qr-image" />
            </div>
            <p class="field-hint login-qr-hint">{{ t("login.qrHint") }}</p>
          </div>
          <div v-else-if="session.qr_expired" class="login-qr-expired">
            <p>{{ t("login.statusQrExpired") }}</p>
            <button
              type="button"
              class="button secondary"
              :disabled="starting"
              @click="emit('refresh')"
            >
              <SvgIcon name="refresh" />{{
                starting ? t("login.refreshing") : t("login.refreshQr")
              }}
            </button>
          </div>

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
                  @click="copyField('url', session.browser_url)"
                >
                  <SvgIcon :name="copiedKind === 'url' ? 'check' : 'copy'" />
                  <span>{{ copiedKind === "url" ? t("login.copied") : t("login.copyUrl") }}</span>
                </button>
              </div>
            </div>

            <div v-if="session.input_code" class="login-copy-block">
              <span class="login-copy-label">{{ t("login.codeLabel") }}</span>
              <div class="login-copy-row">
                <code class="login-code-value">{{ session.input_code }}</code>
                <button
                  type="button"
                  class="button secondary login-copy-btn"
                  @click="copyField('code', session.input_code)"
                >
                  <SvgIcon :name="copiedKind === 'code' ? 'check' : 'copy'" />
                  <span>{{ copiedKind === "code" ? t("login.copied") : t("login.copyCode") }}</span>
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
          <SvgIcon name="refresh" />{{ starting ? t("login.refreshing") : t("login.refreshQr") }}
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
