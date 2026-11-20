<script setup lang="ts">
import type { QuotaView } from "../types/mail";
import SvgIcon from "./SvgIcon.vue";

defineProps<{ quota: QuotaView }>();
</script>

<template>
  <section class="quota-panel" aria-label="账号额度与限制">
    <div class="quota-heading"><span>账号额度与限制</span><SvgIcon name="quota" /></div>
    <dl>
      <div>
        <dt>每日发信</dt>
        <dd>{{ quota.dailySend }}</dd>
      </div>
      <div>
        <dt>API 请求</dt>
        <dd>{{ quota.perMinute }}</dd>
      </div>
      <div>
        <dt></dt>
        <dd>{{ quota.perHour }}</dd>
      </div>
      <div>
        <dt>单附件</dt>
        <dd>{{ quota.singleAttachment }}</dd>
      </div>
      <div>
        <dt>总附件</dt>
        <dd>{{ quota.totalAttachment }}</dd>
      </div>
    </dl>
    <p>当前账号的地址共享额度</p>
  </section>
</template>
