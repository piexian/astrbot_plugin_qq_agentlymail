<script setup lang="ts">
import { onMounted, onUnmounted, useTemplateRef } from "vue";
import { useI18n } from "../composables/useHostContext";
import SvgIcon from "./SvgIcon.vue";

defineProps<{ composeDisabled: boolean }>();
const emit = defineEmits<{ compose: []; refresh: []; openMenu: [] }>();
const query = defineModel<string>({ required: true });
const searchInput = useTemplateRef<HTMLInputElement>("search");
const { t } = useI18n();

function onKeydown(event: KeyboardEvent) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    searchInput.value?.focus();
  }
}

onMounted(() => document.addEventListener("keydown", onKeydown));
onUnmounted(() => document.removeEventListener("keydown", onKeydown));
</script>

<template>
  <header class="topbar">
    <div class="brand-block">
      <button
        class="icon-button mobile-only menu-button"
        :aria-label="t('topbar.openNav')"
        @click="emit('openMenu')"
      >
        <SvgIcon name="menu" />
      </button>
      <span class="brand-mark"><SvgIcon name="mail" /></span>
      <div>
        <strong>{{ t("app.title") }}</strong>
      </div>
    </div>
    <label class="global-search">
      <SvgIcon name="search" />
      <input
        ref="search"
        v-model="query"
        type="search"
        :placeholder="t('topbar.searchPlaceholder')"
        autocomplete="off"
      />
      <kbd>Ctrl K</kbd>
    </label>
    <div class="topbar-actions">
      <button class="icon-button" :aria-label="t('topbar.refresh')" @click="emit('refresh')">
        <SvgIcon name="refresh" />
      </button>
      <button class="button primary" :disabled="composeDisabled" @click="emit('compose')">
        <SvgIcon name="compose" /><span>{{ t("topbar.compose") }}</span>
      </button>
    </div>
  </header>
</template>
