<script setup lang="ts">
import { computed, shallowRef, watch } from "vue";
import { useI18n } from "../composables/useHostContext";
import type { Alias, DisplayAccount } from "../types/mail";
import DialogShell from "./DialogShell.vue";
import SvgIcon from "./SvgIcon.vue";

const ALIAS_DOMAIN = "@agent.qq.com";
const ALIAS_PREFIX_RE = /^[a-z][a-z0-9]{5,17}$/;

const props = defineProps<{
  open: boolean;
  account: DisplayAccount | null;
  aliasLimit: number;
  loading: boolean;
  modifyLoading: boolean;
  meLoading: boolean;
}>();
const emit = defineEmits<{
  close: [];
  create: [prefix?: string];
  modifyAlias: [alias: Alias, prefix: string];
  deleteAlias: [alias: Alias];
}>();
const { t } = useI18n();

const createPrefix = shallowRef("");
const editingAliasId = shallowRef<string | null>(null);
const editPrefix = shallowRef("");

const count = computed(() => props.account?.aliases.length ?? 0);
const subtitle = computed(() =>
  props.account
    ? t("mailboxDialog.subtitle", {
        account: props.account.name,
        count: count.value,
        limit: props.aliasLimit,
      })
    : "",
);
const full = computed(() => count.value >= props.aliasLimit);
const busy = computed(() => props.loading || props.modifyLoading);

function normalizeInput(raw: string): string {
  return raw.replace(/[^0-9a-zA-Z]/g, "").toLowerCase();
}

const createPrefixClean = computed(() => createPrefix.value.trim().toLowerCase());
const createPrefixInvalid = computed(
  () => createPrefixClean.value.length > 0 && !ALIAS_PREFIX_RE.test(createPrefixClean.value),
);

const editingAlias = computed(
  () => props.account?.aliases.find((item) => item.alias_id === editingAliasId.value) ?? null,
);
const editOrigPrefix = computed(() =>
  (editingAlias.value?.email ?? "").split("@")[0].toLowerCase(),
);
const editPrefixClean = computed(() => editPrefix.value.trim().toLowerCase());
const editPrefixInvalid = computed(
  () => editPrefixClean.value.length === 0 || !ALIAS_PREFIX_RE.test(editPrefixClean.value),
);
const editCanSave = computed(
  () =>
    !busy.value &&
    !editPrefixInvalid.value &&
    editPrefixClean.value !== editOrigPrefix.value &&
    editingAlias.value !== null,
);

function onCreateInput(event: Event) {
  const target = event.target as HTMLInputElement;
  const cleaned = normalizeInput(target.value).slice(0, 18);
  createPrefix.value = cleaned;
  target.value = cleaned;
}

function onEditInput(event: Event) {
  const target = event.target as HTMLInputElement;
  const cleaned = normalizeInput(target.value).slice(0, 18);
  editPrefix.value = cleaned;
  target.value = cleaned;
}

function startEdit(alias: Alias) {
  editingAliasId.value = alias.alias_id;
  editPrefix.value = (alias.email ?? "").split("@")[0].toLowerCase();
}

function cancelEdit() {
  editingAliasId.value = null;
  editPrefix.value = "";
}

function submitEdit() {
  if (!editCanSave.value || !editingAlias.value) return;
  emit("modifyAlias", editingAlias.value, editPrefixClean.value);
}

function submitCreate() {
  if (full.value || busy.value || createPrefixInvalid.value) return;
  emit("create", createPrefixClean.value || undefined);
}

watch(
  () => props.open,
  (isOpen) => {
    if (!isOpen) {
      createPrefix.value = "";
      cancelEdit();
    }
  },
);

watch(count, (next, prev) => {
  if (next > prev) createPrefix.value = "";
});

watch(
  () => [editingAliasId.value, editingAlias.value?.email ?? null] as const,
  ([nextId, nextEmail], [prevId, prevEmail]) => {
    if (nextId && nextId === prevId && prevEmail !== null && nextEmail !== prevEmail) {
      cancelEdit();
    }
  },
);
</script>

<template>
  <DialogShell
    :open="open"
    :title="t('mailboxDialog.title')"
    :subtitle="subtitle"
    dialog-class="mailbox-dialog"
    @close="emit('close')"
  >
    <p class="field-hint mailbox-note">{{ t("mailboxDialog.note") }}</p>
    <p v-if="meLoading" class="field-hint mailbox-note">{{ t("mailboxDialog.loadingQuota") }}</p>
    <div class="mailbox-list">
      <div v-for="alias in account?.aliases ?? []" :key="alias.alias_id" class="mailbox-row">
        <span class="avatar small" :data-tone="account?.tone">{{
          (alias.email ?? alias.alias_id).slice(0, 1).toUpperCase()
        }}</span>
        <div v-if="editingAliasId === alias.alias_id" class="mailbox-edit-box">
          <div class="alias-prefix-group" :class="{ invalid: editPrefixInvalid }">
            <input
              :value="editPrefix"
              type="text"
              maxlength="18"
              autocomplete="off"
              spellcheck="false"
              :disabled="busy"
              :placeholder="t('mailboxDialog.editPrefixPlaceholder')"
              @input="onEditInput"
              @keydown.enter.prevent="submitEdit"
              @keydown.esc.stop.prevent="cancelEdit"
            />
            <span class="alias-prefix-suffix">{{ ALIAS_DOMAIN }}</span>
          </div>
          <p class="alias-prefix-hint" :class="{ error: editPrefixInvalid }">
            {{
              editPrefixInvalid ? t("mailboxDialog.prefixInvalid") : t("mailboxDialog.prefixRule")
            }}
          </p>
        </div>
        <div v-else>
          <strong>{{ alias.email ?? alias.alias_id }}</strong>
          <p>
            {{
              alias.is_primary ? t("mailboxDialog.primaryAddress") : t("mailboxDialog.aliasAddress")
            }}
          </p>
        </div>
        <div v-if="editingAliasId === alias.alias_id" class="mailbox-row-actions">
          <button class="button primary" :disabled="!editCanSave" @click="submitEdit">
            {{ modifyLoading ? t("mailboxDialog.saving") : t("mailboxDialog.save") }}
          </button>
          <button class="button ghost" :disabled="busy" @click="cancelEdit">
            {{ t("mailboxDialog.cancel") }}
          </button>
        </div>
        <div v-else class="mailbox-row-actions">
          <button class="button ghost" :disabled="busy" @click="startEdit(alias)">
            {{ t("mailboxDialog.edit") }}
          </button>
          <button class="button secondary" :disabled="busy" @click="emit('deleteAlias', alias)">
            {{ t("mailboxDialog.delete") }}
          </button>
        </div>
      </div>
      <p v-if="count === 0" class="mailbox-empty">{{ t("mailboxDialog.empty") }}</p>
    </div>
    <div v-if="!full" class="mailbox-create-box">
      <div class="alias-prefix-group" :class="{ invalid: createPrefixInvalid }">
        <input
          :value="createPrefix"
          type="text"
          maxlength="18"
          autocomplete="off"
          spellcheck="false"
          :disabled="busy"
          :placeholder="t('mailboxDialog.prefixPlaceholder')"
          @input="onCreateInput"
          @keydown.enter.prevent="submitCreate"
        />
        <span class="alias-prefix-suffix">{{ ALIAS_DOMAIN }}</span>
      </div>
      <p class="alias-prefix-hint" :class="{ error: createPrefixInvalid }">
        {{ createPrefixInvalid ? t("mailboxDialog.prefixInvalid") : t("mailboxDialog.prefixRule") }}
      </p>
    </div>
    <template #footer>
      <button
        class="button secondary"
        :disabled="full || busy || createPrefixInvalid"
        @click="submitCreate"
      >
        <SvgIcon v-if="!full && !loading" name="plus" />{{
          loading
            ? t("mailboxDialog.creating")
            : full
              ? t("mailboxDialog.limitReached", { limit: aliasLimit })
              : t("mailboxDialog.create")
        }}
      </button>
      <button class="button primary" @click="emit('close')">{{ t("mailboxDialog.done") }}</button>
    </template>
  </DialogShell>
</template>
