<script setup lang="ts">
import type { Toast } from "../composables/useToasts";
import SvgIcon from "./SvgIcon.vue";

defineProps<{ toasts: Toast[] }>();
</script>

<template>
  <div class="toast-region" aria-live="polite" aria-atomic="true">
    <div v-for="toast in toasts" :key="toast.id" class="toast">
      <SvgIcon name="check" /><span>{{ toast.message }}</span>
    </div>
  </div>
</template>
