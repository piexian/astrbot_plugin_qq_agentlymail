<script setup lang="ts">
import { onMounted, onUnmounted, useTemplateRef } from "vue";
import SvgIcon from "./SvgIcon.vue";

defineProps<{ composeDisabled: boolean }>();
const emit = defineEmits<{ compose: []; refresh: []; openMenu: [] }>();
const query = defineModel<string>({ required: true });
const searchInput = useTemplateRef<HTMLInputElement>("search");

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
        aria-label="打开邮箱导航"
        @click="emit('openMenu')"
      >
        <SvgIcon name="menu" />
      </button>
      <span class="brand-mark"><SvgIcon name="mail" /></span>
      <div><strong>Agently Mail</strong></div>
    </div>
    <label class="global-search">
      <SvgIcon name="search" />
      <input
        ref="search"
        v-model="query"
        type="search"
        placeholder="搜索当前邮箱的邮件"
        autocomplete="off"
      />
      <kbd>Ctrl K</kbd>
    </label>
    <div class="topbar-actions">
      <button class="icon-button" aria-label="刷新邮件" @click="emit('refresh')">
        <SvgIcon name="refresh" />
      </button>
      <button class="button primary" :disabled="composeDisabled" @click="emit('compose')">
        <SvgIcon name="compose" /><span>写邮件</span>
      </button>
    </div>
  </header>
</template>
