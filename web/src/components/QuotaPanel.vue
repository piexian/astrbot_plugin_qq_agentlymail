<script setup lang="ts">
import { useI18n } from "../composables/useHostContext";
import type { QuotaView } from "../types/mail";
import SvgIcon from "./SvgIcon.vue";

defineProps<{ quota: QuotaView }>();
const { t } = useI18n();
</script>

<template>
  <section class="quota-panel" :aria-label="t('quota.title')">
    <div class="quota-heading">
      <span>{{ t("quota.title") }}</span
      ><SvgIcon name="quota" />
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
</template>
