<script setup lang="ts">
/** 额度信息折叠浮层：默认收起为顶栏按钮，按需展开查看发信、API 与附件限额。 */
import { onMounted, onUnmounted, shallowRef, useTemplateRef } from "vue";
import { useI18n } from "../composables/useHostContext";
import type { QuotaView } from "../types/mail";
import SvgIcon from "./SvgIcon.vue";

defineProps<{ quota: QuotaView }>();
const { t } = useI18n();

const open = shallowRef(false);
const rootRef = useTemplateRef<HTMLDivElement>("root");

function toggle() {
  open.value = !open.value;
}

function close() {
  open.value = false;
}

function onPointerDown(event: MouseEvent) {
  if (!open.value) return;
  const target = event.target as Node | null;
  if (target && rootRef.value?.contains(target)) return;
  close();
}

function onKeydown(event: KeyboardEvent) {
  if (open.value && event.key === "Escape") {
    close();
  }
}

onMounted(() => {
  document.addEventListener("mousedown", onPointerDown);
  document.addEventListener("keydown", onKeydown);
});
onUnmounted(() => {
  document.removeEventListener("mousedown", onPointerDown);
  document.removeEventListener("keydown", onKeydown);
});
</script>

<template>
  <div ref="root" class="quota-popover-wrap">
    <button
      type="button"
      class="quota-trigger"
      :class="{ active: open }"
      :aria-expanded="open"
      aria-haspopup="true"
      :aria-label="t('quota.title')"
      :title="t('quota.title')"
      @click="toggle"
    >
      <SvgIcon name="quota" />
      <span class="quota-trigger-label">{{ t("topbar.quota") }}</span>
      <SvgIcon name="chevron" class="quota-chevron" :class="{ open }" />
    </button>
    <section v-if="open" class="quota-popover" role="region" :aria-label="t('quota.title')">
      <div class="quota-heading">
        <span>{{ t("quota.title") }}</span>
        <SvgIcon name="quota" />
      </div>
      <dl>
        <div>
          <dt>{{ t("quota.dailySend") }}</dt>
          <dd>{{ quota.dailySend }}</dd>
        </div>
        <div>
          <dt>{{ t("quota.apiRequests") }}</dt>
          <dd>{{ quota.perMinute }}</dd>
        </div>
        <div>
          <dt></dt>
          <dd>{{ quota.perHour }}</dd>
        </div>
        <div>
          <dt>{{ t("quota.singleAttachment") }}</dt>
          <dd>{{ quota.singleAttachment }}</dd>
        </div>
        <div>
          <dt>{{ t("quota.totalAttachment") }}</dt>
          <dd>{{ quota.totalAttachment }}</dd>
        </div>
      </dl>
      <p>{{ t("quota.sharedNote") }}</p>
    </section>
  </div>
</template>
