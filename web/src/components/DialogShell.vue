<script setup lang="ts">
import { useTemplateRef, watch } from "vue";
import SvgIcon from "./SvgIcon.vue";

const props = defineProps<{
  open: boolean;
  title: string;
  subtitle?: string;
  dialogClass?: string;
  footerEnd?: boolean;
}>();
const emit = defineEmits<{ close: [] }>();
const dialogRef = useTemplateRef<HTMLDialogElement>("dialog");

watch(
  () => props.open,
  (value) => {
    const element = dialogRef.value;
    if (!element) return;
    if (value && !element.open) element.showModal();
    else if (!value && element.open) element.close();
  },
);
</script>

<template>
  <dialog
    ref="dialog"
    :class="['dialog', dialogClass]"
    :aria-label="title"
    @cancel.prevent="emit('close')"
  >
    <div class="dialog-header">
      <div>
        <h2>{{ title }}</h2>
        <p v-if="subtitle">{{ subtitle }}</p>
      </div>
      <button class="icon-button" aria-label="关闭" @click="emit('close')">
        <SvgIcon name="close" />
      </button>
    </div>
    <slot />
    <div v-if="$slots.footer" class="dialog-footer" :class="{ end: footerEnd }">
      <slot name="footer" />
    </div>
  </dialog>
</template>
