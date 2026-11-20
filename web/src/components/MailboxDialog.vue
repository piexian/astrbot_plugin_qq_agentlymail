<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "../composables/useHostContext";
import type { Alias, DisplayAccount } from "../types/mail";
import DialogShell from "./DialogShell.vue";
import SvgIcon from "./SvgIcon.vue";

const props = defineProps<{
  open: boolean;
  account: DisplayAccount | null;
  aliasLimit: number;
  loading: boolean;
  meLoading: boolean;
}>();
const emit = defineEmits<{ close: []; create: []; deleteAlias: [alias: Alias] }>();
const { t } = useI18n();

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
        <div>
          <strong>{{ alias.email ?? alias.alias_id }}</strong>
          <p>
            {{
              alias.is_primary ? t("mailboxDialog.primaryAddress") : t("mailboxDialog.aliasAddress")
            }}
          </p>
        </div>
        <button class="button secondary" :disabled="loading" @click="emit('deleteAlias', alias)">
          {{ t("mailboxDialog.delete") }}
        </button>
      </div>
      <p v-if="count === 0" class="mailbox-empty">{{ t("mailboxDialog.empty") }}</p>
    </div>
    <template #footer>
      <button class="button secondary" :disabled="full || loading" @click="emit('create')">
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
