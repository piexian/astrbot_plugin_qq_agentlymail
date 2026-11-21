<script setup lang="ts">
import { computed, onMounted, onUnmounted, shallowRef, watch } from "vue";
import { getBridge } from "./api/bridge";
import { ALIAS_LIMIT_FALLBACK, createMailApi, type MailApi } from "./api/mailApi";
import { useAccounts } from "./composables/useAccounts";
import { useCompose, type ComposeMode } from "./composables/useCompose";
import { applyHostContext, syncHostContext, useI18n } from "./composables/useHostContext";
import { useLogin } from "./composables/useLogin";
import { useMessageList } from "./composables/useMessageList";
import { useReader } from "./composables/useReader";
import { useToasts } from "./composables/useToasts";
import type { MessageKey } from "./i18n/messages";
import type { Alias, Attachment, FolderId, Me } from "./types/mail";
import { errorMessage } from "./utils/error";
import IconSprite from "./components/IconSprite.vue";
import SvgIcon from "./components/SvgIcon.vue";
import TopBar from "./components/TopBar.vue";
import MessageListPane from "./components/MessageListPane.vue";
import ReaderPane from "./components/ReaderPane.vue";
import AccountDialog from "./components/AccountDialog.vue";
import LoginDialog from "./components/LoginDialog.vue";
import MailboxDialog from "./components/MailboxDialog.vue";
import DeleteAliasDialog from "./components/DeleteAliasDialog.vue";
import ComposeDialog from "./components/ComposeDialog.vue";
import SendConfirmDialog from "./components/SendConfirmDialog.vue";
import TrashConfirmDialog from "./components/TrashConfirmDialog.vue";
import ToastRegion from "./components/ToastRegion.vue";

const { t } = useI18n();
const { toasts, notify } = useToasts();

const bridge = getBridge();
let stopHostSync: (() => void) | null = null;
// bridge 缺失时不构造真实客户端，也不回退 mock；初始化错误态直接渲染。
const mailApi = (bridge ? createMailApi(bridge) : null) as MailApi;
const initErrorKey = shallowRef<MessageKey | null>(bridge ? null : "app.bridgeMissing");
const initErrorRaw = shallowRef<string | null>(null);
const initError = computed(() => (initErrorKey.value ? t(initErrorKey.value) : initErrorRaw.value));
const initializing = shallowRef(true);

const accountStore = useAccounts(mailApi, notify);
const {
  accounts,
  accountId,
  currentAccount,
  currentAlias,
  quotaView,
  accountsLoading,
  accountsError,
  loadAccounts,
  loadMe,
  selectAddress,
  selectDefaultAccount,
  createAlias,
  modifyAlias,
  deleteAlias,
} = accountStore;

const selectedMessageId = shallowRef<string | null>(null);
const mobileReading = shallowRef(false);
const listCollapsed = shallowRef(false);

const list = useMessageList({
  api: mailApi,
  account: () => currentAccount.value?.account ?? null,
  alias: () => currentAlias.value?.alias_id ?? null,
});
const {
  folder,
  filter,
  query,
  searchMode,
  items: messages,
  hasMore,
  loading: listLoading,
  loadingMore,
  error: listError,
  setFolder,
  clearQuery,
  loadMore,
  reload: reloadList,
  resetView,
} = list;

const reader = useReader({
  api: mailApi,
  account: () => currentAccount.value?.account ?? null,
  alias: () => currentAlias.value?.alias_id ?? null,
  messageId: () => selectedMessageId.value,
});
const {
  message: fullMessage,
  loading: readerLoading,
  error: readerError,
  retry: retryReader,
} = reader;

const compose = useCompose({
  api: mailApi,
  notify,
  getAccount: () => currentAccount.value,
  getCurrentAlias: () => currentAlias.value,
  getSelectedMessage: () => fullMessage.value,
  onSent: handleSent,
});
const {
  open: composeOpen,
  confirmOpen,
  mode: composeMode,
  form: composeForm,
  attachments: composeAttachments,
  forwardAttachments,
  pendingSend,
  sending,
  sendError,
  resultUnknown,
  retryAcknowledged,
  openCompose,
  close: closeCompose,
  addFiles,
  removeAttachment,
  saveDraft,
  submit,
  confirmSend,
  acknowledgeRetry,
  cancelConfirm,
} = compose;

const login = useLogin({
  api: mailApi,
  notify,
  onLoggedIn: handleLoggedIn,
});
const {
  open: loginOpen,
  accountInput: loginAccount,
  session: loginSession,
  starting: loginStarting,
  cancelling: loginCancelling,
  localError: loginError,
  isInFlight: loginInFlight,
  openLogin,
  startLogin,
  refreshLink,
  cancelLogin,
  closeLogin,
} = login;

const accountDialogOpen = shallowRef(false);
const mailboxDialogOpen = shallowRef(false);
const manageAccountId = shallowRef<string | null>(null);
const deleteTarget = shallowRef<Alias | null>(null);
const managedMe = shallowRef<Me | null>(null);
const managedMeLoading = shallowRef(false);
const createLoading = shallowRef(false);
const modifyLoading = shallowRef(false);
const deleteLoading = shallowRef(false);
const trashLoading = shallowRef(false);
const trashConfirmOpen = shallowRef(false);
const trashError = shallowRef<string | null>(null);
const downloading = shallowRef(false);
let managedMeToken = 0;

const managedAccount = computed(
  () => accounts.value.find((item) => item.account === manageAccountId.value) ?? null,
);
const managedAliasLimit = computed(
  () => managedMe.value?.account_info?.agent_alias_limit ?? ALIAS_LIMIT_FALLBACK,
);
const composeDisabled = computed(
  () => !currentAlias.value || currentAccount.value?.status !== "ok",
);
const listSummary = computed(() => {
  const count = messages.value.length;
  if (searchMode.value) return t("list.summarySearch", { query: query.value.trim(), count });
  return hasMore.value ? t("list.summaryMore", { count }) : t("list.summaryLoaded", { count });
});

function withTimeout<T>(promise: Promise<T>, ms: number, message: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error(message)), ms);
    promise.then(
      (value) => {
        window.clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        window.clearTimeout(timer);
        reject(error);
      },
    );
  });
}

async function initialize() {
  if (!bridge) return;
  initializing.value = true;
  initErrorKey.value = null;
  initErrorRaw.value = null;
  try {
    const ctx = await withTimeout(bridge.ready(), 10000, t("app.bridgeTimeout"));
    applyHostContext(ctx);
    await loadAccounts();
    selectDefaultAccount();
  } catch (error) {
    initErrorRaw.value = errorMessage(error);
  } finally {
    initializing.value = false;
  }
}

async function retryAccounts() {
  await loadAccounts();
  selectDefaultAccount();
}

function openAccountDialog() {
  accountDialogOpen.value = true;
}

function onSelectFolder(next: FolderId) {
  setFolder(next);
  listCollapsed.value = false;
  mobileReading.value = false;
}

function onSelectAddress(nextAccountId: string, nextAliasId: string) {
  selectAddress(nextAccountId, nextAliasId);
  resetView();
  selectedMessageId.value = null;
  mobileReading.value = false;
  listCollapsed.value = false;
  accountDialogOpen.value = false;
  notify(t("app.switchedTo", { target: currentAlias.value?.email ?? nextAliasId }));
}

// 切账号或别名时清理阅读器与移动端状态，列表刷新由 useMessageList 的筛选键负责。
watch([accountId, () => currentAlias.value?.alias_id ?? null], () => {
  selectedMessageId.value = null;
  mobileReading.value = false;
  listCollapsed.value = false;
});

// 进入搜索态时自动展开列表，避免折叠状态下看不到搜索结果。
watch(searchMode, (active) => {
  if (active) {
    listCollapsed.value = false;
    mobileReading.value = false;
  }
});

async function loadManagedMe(account: string) {
  const token = ++managedMeToken;
  managedMeLoading.value = true;
  managedMe.value = null;
  try {
    const result = await mailApi.me(account);
    if (token === managedMeToken) managedMe.value = result;
  } catch {
    if (token === managedMeToken) managedMe.value = null;
  } finally {
    if (token === managedMeToken) managedMeLoading.value = false;
  }
}

function onManage(nextAccountId: string) {
  manageAccountId.value = nextAccountId;
  accountDialogOpen.value = false;
  mailboxDialogOpen.value = true;
  void loadManagedMe(nextAccountId);
}

async function handleLoggedIn(loggedAccount: string) {
  await loadAccounts();
  const target = accounts.value.find((item) => item.account === loggedAccount);
  if (target && target.aliases.length > 0) {
    const primary = target.aliases.find((alias) => alias.is_primary) ?? target.aliases[0];
    selectAddress(target.account, primary.alias_id);
    resetView();
    selectedMessageId.value = null;
    mobileReading.value = false;
    listCollapsed.value = false;
  } else {
    selectDefaultAccount();
  }
}

function onAddAccount() {
  accountDialogOpen.value = false;
  openLogin("default", false);
}

function onRelogin(targetAccount?: string) {
  accountDialogOpen.value = false;
  openLogin(targetAccount || "default", Boolean(targetAccount));
}

async function onCreateAlias(prefix?: string) {
  const account = manageAccountId.value;
  if (!account || createLoading.value) return;
  createLoading.value = true;
  try {
    await createAlias(account, prefix);
    if (account === accountId.value) {
      resetView();
      selectedMessageId.value = null;
    }
  } catch (error) {
    notify(errorMessage(error));
  } finally {
    createLoading.value = false;
  }
}

async function onModifyAlias(target: Alias, prefix: string) {
  const account = manageAccountId.value;
  if (!account || modifyLoading.value) return;
  modifyLoading.value = true;
  try {
    await modifyAlias(account, target.alias_id, prefix);
  } catch (error) {
    notify(errorMessage(error));
  } finally {
    modifyLoading.value = false;
  }
}

function onRequestDelete(alias: Alias) {
  deleteTarget.value = alias;
}

async function onConfirmDelete() {
  const target = deleteTarget.value;
  const account = manageAccountId.value;
  deleteTarget.value = null;
  if (!target || !account || deleteLoading.value) return;
  deleteLoading.value = true;
  try {
    await deleteAlias(account, target.alias_id);
    if (account === accountId.value) {
      resetView();
      selectedMessageId.value = null;
      mobileReading.value = false;
    }
  } catch (error) {
    notify(errorMessage(error));
  } finally {
    deleteLoading.value = false;
  }
}

async function onDownload(attachment: Attachment, index: number) {
  const account = currentAccount.value?.account;
  const alias = currentAlias.value?.alias_id;
  const messageId = fullMessage.value?.message_id;
  if (!account || !alias || !messageId || downloading.value) return;
  downloading.value = true;
  try {
    const ref =
      attachment.attachment_id != null
        ? {
            account,
            alias_id: alias,
            message_id: messageId,
            attachment_id: attachment.attachment_id,
          }
        : { account, alias_id: alias, message_id: messageId, index };
    await mailApi.downloadAttachment(ref, attachment.filename ?? undefined);
  } catch {
    notify(t("app.downloadFailed"));
  } finally {
    downloading.value = false;
  }
}

function onOpenCompose(mode: ComposeMode) {
  openCompose(mode);
}

function onSelectMessage(messageId: string) {
  selectedMessageId.value = messageId;
  if (window.innerWidth <= 760) mobileReading.value = true;
}

function exitReading() {
  mobileReading.value = false;
}

function onRequestTrash() {
  const message = fullMessage.value;
  if (!message) return;
  if (folder.value === "trash") {
    notify(t("app.alreadyInTrash"));
    return;
  }
  trashError.value = null;
  trashConfirmOpen.value = true;
}

function closeTrashConfirm() {
  if (trashLoading.value) return;
  trashConfirmOpen.value = false;
  trashError.value = null;
}

async function confirmTrash() {
  const message = fullMessage.value;
  const account = currentAccount.value?.account;
  const alias = currentAlias.value?.alias_id;
  if (!message || !account || !alias || trashLoading.value) return;
  trashLoading.value = true;
  try {
    await mailApi.trash({ account, alias_id: alias, message_id: message.message_id });
    trashConfirmOpen.value = false;
    trashError.value = null;
    selectedMessageId.value = null;
    mobileReading.value = false;
    reloadList();
    notify(t("app.movedToTrash"));
  } catch (error) {
    trashError.value = errorMessage(error);
  } finally {
    trashLoading.value = false;
  }
}

function handleSent() {
  resetView();
  setFolder("sent");
  reloadList();
  selectedMessageId.value = null;
  mobileReading.value = false;
  listCollapsed.value = false;
  const id = accountId.value;
  if (id) void loadMe(id);
}

function handleCheckSent() {
  cancelConfirm();
  closeCompose();
  resetView();
  setFolder("sent");
  reloadList();
  selectedMessageId.value = null;
  mobileReading.value = false;
  listCollapsed.value = false;
}

function refresh() {
  reloadList();
}

function reloadPage() {
  window.location.reload();
}

function onResize() {
  if (window.innerWidth > 760) exitReading();
}

onMounted(() => {
  stopHostSync = syncHostContext(bridge);
  void initialize();
  window.addEventListener("resize", onResize);
});
onUnmounted(() => {
  stopHostSync?.();
  stopHostSync = null;
  window.removeEventListener("resize", onResize);
});
</script>

<template>
  <IconSprite />
  <div v-if="initError" class="init-screen" role="alert">
    <div class="init-card">
      <SvgIcon name="mail" />
      <strong>{{ t("app.initFailedTitle") }}</strong>
      <p>{{ initError }}</p>
      <button type="button" class="button primary" @click="reloadPage">
        {{ t("app.reloadPage") }}
      </button>
    </div>
  </div>
  <div v-else-if="initializing" class="init-screen">
    <div class="init-card">
      <SvgIcon name="refresh" />
      <strong>{{ t("app.initializing") }}</strong>
    </div>
  </div>
  <template v-else>
    <div class="app-shell">
      <TopBar
        v-model="query"
        :account="currentAccount"
        :alias="currentAlias"
        :folder="folder"
        :search-mode="searchMode"
        :inbox-unread="0"
        :quota="quotaView"
        :compose-disabled="composeDisabled"
        @open-account="openAccountDialog"
        @select-folder="onSelectFolder"
        @compose="onOpenCompose('new')"
        @refresh="refresh"
      />
      <div
        class="mail-layout"
        :class="{ 'mobile-reading': mobileReading, 'list-collapsed': listCollapsed }"
      >
        <MessageListPane
          v-model:filter="filter"
          :account="currentAccount"
          :alias="currentAlias"
          :folder="folder"
          :messages="messages"
          :selected-id="selectedMessageId"
          :summary="listSummary"
          :loading="listLoading"
          :loading-more="loadingMore"
          :has-more="hasMore"
          :error="listError"
          :search-mode="searchMode"
          :accounts-loading="accountsLoading"
          :accounts-error="accountsError"
          :account-count="accounts.length"
          @select-message="onSelectMessage"
          @open-account="openAccountDialog"
          @open-login="onRelogin"
          @load-more="loadMore"
          @retry="reloadList"
          @retry-accounts="retryAccounts"
          @clear-search="clearQuery"
        />
        <ReaderPane
          :message="fullMessage"
          :address="currentAlias?.email ?? null"
          :loading="readerLoading"
          :error="readerError"
          :trash-loading="trashLoading"
          :downloading="downloading"
          :list-collapsed="listCollapsed"
          @back="exitReading"
          @toggle-list="listCollapsed = !listCollapsed"
          @reply="onOpenCompose('reply')"
          @forward="onOpenCompose('forward')"
          @trash="onRequestTrash"
          @download="onDownload"
          @retry="retryReader"
        />
      </div>
    </div>

    <AccountDialog
      :open="accountDialogOpen"
      :accounts="accounts"
      :current-account-id="accountId"
      :current-alias-id="currentAlias?.alias_id ?? null"
      @close="accountDialogOpen = false"
      @select-address="onSelectAddress"
      @manage="onManage"
      @add-account="onAddAccount"
      @relogin="onRelogin"
    />
    <LoginDialog
      v-model:account="loginAccount"
      :open="loginOpen"
      :session="loginSession"
      :starting="loginStarting"
      :cancelling="loginCancelling"
      :local-error="loginError"
      :is-in-flight="loginInFlight"
      @close="closeLogin"
      @start="startLogin"
      @refresh="refreshLink"
      @abort="cancelLogin()"
      @copied="notify"
    />
    <MailboxDialog
      :open="mailboxDialogOpen"
      :account="managedAccount"
      :alias-limit="managedAliasLimit"
      :loading="createLoading"
      :modify-loading="modifyLoading"
      :me-loading="managedMeLoading"
      @close="mailboxDialogOpen = false"
      @create="onCreateAlias"
      @modify-alias="onModifyAlias"
      @delete-alias="onRequestDelete"
    />
    <DeleteAliasDialog
      :open="deleteTarget !== null"
      :alias="deleteTarget"
      :account-name="managedAccount?.name ?? ''"
      :loading="deleteLoading"
      @close="deleteTarget = null"
      @confirm="onConfirmDelete"
    />
    <ComposeDialog
      v-model:from-alias-id="composeForm.fromAliasId"
      v-model:to="composeForm.to"
      v-model:cc="composeForm.cc"
      v-model:subject="composeForm.subject"
      v-model:body="composeForm.body"
      v-model:format="composeForm.format"
      :open="composeOpen"
      :mode="composeMode"
      :aliases="currentAccount?.aliases ?? []"
      :attachments="composeAttachments"
      :forward-attachments="forwardAttachments"
      :sending="sending"
      @close="closeCompose"
      @add-files="addFiles"
      @remove-attachment="removeAttachment"
      @save-draft="saveDraft"
      @submit="submit"
    />
    <TrashConfirmDialog
      :open="trashConfirmOpen"
      :subject="fullMessage?.subject ?? ''"
      :loading="trashLoading"
      :error="trashError"
      @close="closeTrashConfirm"
      @confirm="confirmTrash"
    />
    <SendConfirmDialog
      :open="confirmOpen"
      :pending="pendingSend"
      :sending="sending"
      :error="sendError"
      :result-unknown="resultUnknown"
      :retry-acknowledged="retryAcknowledged"
      @close="cancelConfirm"
      @confirm="confirmSend"
      @acknowledge-retry="acknowledgeRetry"
      @check-sent="handleCheckSent"
    />
  </template>
  <ToastRegion :toasts="toasts" />
</template>
