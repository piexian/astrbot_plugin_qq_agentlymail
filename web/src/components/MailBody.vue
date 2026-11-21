<script setup lang="ts">
/** 邮件正文：PLAIN 原样保留换行；MARKDOWN 关闭内联 HTML 并经白名单清理；HTML 在 Shadow DOM 中隔离渲染。 */
import { computed, useTemplateRef, watch } from "vue";
import { useI18n } from "../composables/useHostContext";
import {
  normalizeBodyFormat,
  prepareHtmlEmail,
  renderMarkdownBody,
  SHADOW_BASE_CSS,
  SHADOW_GUARD_CSS,
} from "../utils/mailHtml";

const props = defineProps<{
  body: string;
  bodyFormat: string;
}>();

const { isDark } = useI18n();
const htmlHost = useTemplateRef<HTMLDivElement>("htmlHost");

const format = computed(() => normalizeBodyFormat(props.bodyFormat));
const renderedMarkdown = computed(() =>
  format.value === "MARKDOWN" ? renderMarkdownBody(props.body) : "",
);
const preparedHtml = computed(() =>
  format.value === "HTML" ? prepareHtmlEmail(props.body) : null,
);

function renderHtmlInShadow() {
  const host = htmlHost.value;
  const prepared = preparedHtml.value;
  if (!host || !prepared) return;

  const shadow = host.shadowRoot ?? host.attachShadow({ mode: "open" });

  const styleEl = document.createElement("style");
  styleEl.textContent = prepared.css
    ? `${SHADOW_BASE_CSS}\n${prepared.css}\n${SHADOW_GUARD_CSS}`
    : `${SHADOW_BASE_CSS}\n${SHADOW_GUARD_CSS}`;

  const rootEl = document.createElement("div");
  rootEl.className =
    isDark.value && prepared.hasTemplateStyling
      ? "agently-mail-root mail-paper"
      : "agently-mail-root";

  const bodyEl = document.createElement("div");
  bodyEl.className = prepared.bodyAttrs.className
    ? `agently-mail-body ${prepared.bodyAttrs.className}`
    : "agently-mail-body";
  if (prepared.bodyAttrs.style) bodyEl.setAttribute("style", prepared.bodyAttrs.style);
  if (prepared.bodyAttrs.dir) bodyEl.setAttribute("dir", prepared.bodyAttrs.dir);
  if (prepared.bodyAttrs.lang) bodyEl.setAttribute("lang", prepared.bodyAttrs.lang);
  bodyEl.innerHTML = prepared.bodyHtml;

  rootEl.appendChild(bodyEl);
  shadow.replaceChildren(styleEl, rootEl);
}

watch([htmlHost, preparedHtml, isDark], renderHtmlInShadow, { flush: "post", immediate: true });
</script>

<template>
  <div v-if="format === 'PLAIN'" class="mail-body-text">{{ body }}</div>
  <div
    v-else-if="format === 'MARKDOWN'"
    class="mail-body-html mail-body-markdown"
    v-html="renderedMarkdown"
  ></div>
  <div v-else ref="htmlHost" class="mail-body-frame"></div>
</template>
