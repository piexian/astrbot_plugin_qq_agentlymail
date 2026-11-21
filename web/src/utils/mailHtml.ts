/** 邮件正文解析与安全清理：支持 PLAIN、MARKDOWN 与带模板样式的 HTML（Shadow DOM 隔离渲染）。 */

import DOMPurify from "dompurify";
import MarkdownIt from "markdown-it-ts";

export type NormalizedBodyFormat = "PLAIN" | "MARKDOWN" | "HTML";

export interface PreparedHtmlEmail {
  bodyHtml: string;
  css: string;
  hasTemplateStyling: boolean;
  bodyAttrs: {
    className: string;
    style: string;
    dir: string | null;
    lang: string | null;
  };
}

const markdown = new MarkdownIt({ html: false, breaks: true, linkify: true });

const MARKDOWN_ALLOWED_TAGS = [
  "a",
  "b",
  "blockquote",
  "body",
  "br",
  "code",
  "dd",
  "del",
  "div",
  "dl",
  "dt",
  "em",
  "figcaption",
  "figure",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "hr",
  "i",
  "img",
  "ins",
  "li",
  "mark",
  "ol",
  "p",
  "pre",
  "s",
  "small",
  "span",
  "strong",
  "sub",
  "sup",
  "table",
  "tbody",
  "td",
  "tfoot",
  "th",
  "thead",
  "tr",
  "u",
  "ul",
];

const MARKDOWN_ALLOWED_ATTR = [
  "alt",
  "colspan",
  "height",
  "href",
  "rel",
  "rowspan",
  "src",
  "target",
  "title",
  "width",
];

const HTML_ADD_ATTR = [
  "align",
  "background",
  "bgcolor",
  "border",
  "cellpadding",
  "cellspacing",
  "color",
  "dir",
  "face",
  "hidden",
  "lang",
  "rel",
  "role",
  "size",
  "target",
  "valign",
];

const HTML_FORBID_TAGS = [
  "animate",
  "animatecolor",
  "animatemotion",
  "animatetransform",
  "applet",
  "audio",
  "base",
  "button",
  "canvas",
  "dialog",
  "embed",
  "feimage",
  "fieldset",
  "foreignobject",
  "form",
  "frame",
  "frameset",
  "head",
  "iframe",
  "image",
  "input",
  "label",
  "legend",
  "link",
  "math",
  "meta",
  "noembed",
  "noframes",
  "noscript",
  "object",
  "optgroup",
  "option",
  "plaintext",
  "portal",
  "script",
  "select",
  "set",
  "style",
  "template",
  "textarea",
  "title",
  "use",
  "video",
  "xmp",
];

const HTML_FORBID_ATTR = [
  "action",
  "autofocus",
  "autoplay",
  "contenteditable",
  "draggable",
  "formaction",
  "ping",
  "poster",
  "srcdoc",
  "srcset",
  "xlink:href",
];

const SAFE_DATA_IMAGE_RE = /^data:image\/(?:png|jpeg|jpg|gif|webp|avif);base64,[A-Za-z0-9+/=\s]+$/i;
const DANGEROUS_CSS_VALUE_RE =
  /expression\s*\(|javascript\s*:|vbscript\s*:|-moz-binding|@import|image-set\s*\(|cross-fade\s*\(|element\s*\(/i;
const CSS_URL_CALL_RE = /url\s*\(\s*(?:"([^"]*)"|'([^']*)'|([^)"'\s]*))\s*\)/gi;
const URL_ALLOWED_CSS_PROPS = new Set([
  "background",
  "background-image",
  "list-style",
  "list-style-image",
]);
const FRAGMENT_URL_CSS_PROPS = new Set(["clip-path", "fill", "mask", "stroke"]);
const LOCAL_FRAGMENT_REF_RE = /^#[A-Za-z0-9_-]+$/;
const BLOCKED_CSS_PROPS = new Set(["behavior", "-moz-binding"]);
const ROOT_HIDDEN_CSS_PROPS = new Set(["overflow", "overflow-y", "max-height"]);
const VALID_COLOR_RE =
  /^(?:#[0-9a-f]{3,8}|[a-z]{3,24}|rgba?\(\s*[0-9.,%\s/]+\)|hsla?\(\s*[0-9.,%\s/]+\))$/i;
const BARE_HEX_COLOR_RE = /^[0-9a-f]{3}(?:[0-9a-f]{3})?$/i;
const SAFE_CLASS_TOKEN_RE = /^[A-Za-z0-9_-]+$/;
const SAFE_LANG_RE = /^[A-Za-z0-9-]+$/;

const ROOT_SELECTOR_RE = /(^|[\s,>+~(])html(?=$|[\s,>+~).#:[\]])/gi;
const BODY_SELECTOR_RE = /(^|[\s,>+~(])body(?=$|[\s,>+~).#:[\]])/gi;
const CSS_ROOT_PSEUDO_RE = /:root(?=$|[\s,>+~).#:[\]])/gi;
const DIRECT_DOC_WRAPPER_SELECTOR_RE = /^\.agently-mail-(?:root|body)$/;

let markdownSanitizer: ReturnType<typeof DOMPurify> | null = null;
let htmlSanitizer: ReturnType<typeof DOMPurify> | null = null;
let styleScratchEl: HTMLElement | null = null;

function stripControlChars(value: string): string {
  let out = "";
  for (let i = 0; i < value.length; i += 1) {
    const code = value.charCodeAt(i);
    if (code > 32 && code !== 127) out += value[i];
  }
  return out;
}

export function isSafeLinkUrl(raw: string): boolean {
  const trimmed = raw.trim();
  if (!trimmed) return false;
  if (trimmed.startsWith("#")) return true;
  const compact = stripControlChars(trimmed);
  try {
    const parsed = new URL(compact);
    return (
      parsed.protocol === "https:" ||
      parsed.protocol === "http:" ||
      parsed.protocol === "mailto:" ||
      parsed.protocol === "tel:"
    );
  } catch {
    return false;
  }
}

export function isSafeImageUrl(raw: string): boolean {
  const trimmed = raw.trim();
  if (!trimmed) return false;
  if (SAFE_DATA_IMAGE_RE.test(trimmed)) return true;
  const compact = stripControlChars(trimmed);
  try {
    const parsed = new URL(compact);
    return parsed.protocol === "https:" || parsed.protocol === "http:";
  } catch {
    return false;
  }
}

function normalizeLegacyColor(raw: string | null): string | null {
  if (!raw) return null;
  const trimmed = raw.trim();
  if (!trimmed) return null;
  if (BARE_HEX_COLOR_RE.test(trimmed)) return `#${trimmed}`;
  if (VALID_COLOR_RE.test(trimmed)) return trimmed;
  return null;
}

function sanitizeCssValue(prop: string, value: string, isRootWrapper: boolean): string | null {
  const lowerProp = prop.toLowerCase();
  if (BLOCKED_CSS_PROPS.has(lowerProp)) return null;
  const trimmed = value.trim();
  if (!trimmed || DANGEROUS_CSS_VALUE_RE.test(trimmed)) return null;

  if (lowerProp === "position") {
    const lowerVal = trimmed.toLowerCase();
    if (lowerVal === "fixed" || lowerVal === "sticky") return "relative";
  }

  if (isRootWrapper) {
    const lowerVal = trimmed.toLowerCase();
    if (lowerProp === "display" && lowerVal === "none") return null;
    if (lowerProp === "visibility" && lowerVal === "hidden") return null;
    if (lowerProp === "opacity" && Number(lowerVal) === 0) return null;
    if (
      ROOT_HIDDEN_CSS_PROPS.has(lowerProp) &&
      (lowerVal === "hidden" || lowerVal === "0" || lowerVal === "0px")
    ) {
      return null;
    }
  }

  if (/url\s*\(/i.test(trimmed)) {
    const allowsImageUrl = URL_ALLOWED_CSS_PROPS.has(lowerProp);
    const allowsFragmentUrl = FRAGMENT_URL_CSS_PROPS.has(lowerProp);
    if (!allowsImageUrl && !allowsFragmentUrl) return null;
    let allSafe = true;
    const remainder = trimmed.replace(CSS_URL_CALL_RE, (_match, dq, sq, bare) => {
      const candidate = (dq ?? sq ?? bare ?? "").trim();
      const ok =
        (allowsImageUrl && isSafeImageUrl(candidate)) ||
        (allowsFragmentUrl && LOCAL_FRAGMENT_REF_RE.test(candidate));
      if (!ok) allSafe = false;
      return "";
    });
    if (!allSafe || /url\s*\(/i.test(remainder)) return null;
  }

  return trimmed;
}

function sanitizeStyleDeclaration(style: CSSStyleDeclaration, isRootWrapper: boolean): string {
  const decls: string[] = [];
  for (let i = 0; i < style.length; i += 1) {
    const prop = style[i];
    if (!prop) continue;
    const rawVal = style.getPropertyValue(prop);
    const safeVal = sanitizeCssValue(prop, rawVal, isRootWrapper);
    if (safeVal === null) continue;
    const priority = style.getPropertyPriority(prop);
    decls.push(`${prop}:${safeVal}${priority ? " !important" : ""}`);
  }
  return decls.join(";");
}

function getStyleScratchElement(): HTMLElement {
  if (!styleScratchEl) {
    const inertDoc = document.implementation.createHTMLDocument("");
    styleScratchEl = inertDoc.createElement("div");
  }
  return styleScratchEl;
}

function sanitizeInlineStyleText(rawStyle: string, isRootWrapper = false): string {
  const scratch = getStyleScratchElement();
  scratch.setAttribute("style", "");
  scratch.style.cssText = rawStyle;
  return sanitizeStyleDeclaration(scratch.style, isRootWrapper);
}

function rewriteSelectorText(selectorText: string): string {
  return selectorText
    .split(",")
    .map((part) =>
      part
        .trim()
        .replace(CSS_ROOT_PSEUDO_RE, ".agently-mail-root")
        .replace(ROOT_SELECTOR_RE, "$1.agently-mail-root")
        .replace(BODY_SELECTOR_RE, "$1.agently-mail-body"),
    )
    .filter(Boolean)
    .join(", ");
}

function isDirectWrapperSelector(selectorText: string): boolean {
  return selectorText
    .split(",")
    .some((part) => DIRECT_DOC_WRAPPER_SELECTOR_RE.test(part.trim().toLowerCase()));
}

function parseCssRules(cssText: string): CSSRuleList | null {
  try {
    if (typeof CSSStyleSheet === "function") {
      const sheet = new CSSStyleSheet();
      sheet.replaceSync(cssText);
      return sheet.cssRules;
    }
  } catch {
    // 浏览器不支持可构造样式表时退回惰性文档。
  }
  try {
    const inertDoc = document.implementation.createHTMLDocument("");
    const styleEl = inertDoc.createElement("style");
    styleEl.textContent = cssText;
    inertDoc.head.appendChild(styleEl);
    return styleEl.sheet?.cssRules ?? null;
  } catch {
    return null;
  }
}

function serializeSanitizedRules(rules: CSSRuleList, stripDarkMedia: boolean): string {
  const chunks: string[] = [];
  for (let i = 0; i < rules.length; i += 1) {
    const rule = rules[i];
    if (!rule) continue;
    if (rule.type === CSSRule.STYLE_RULE) {
      const styleRule = rule as CSSStyleRule;
      const selector = rewriteSelectorText(styleRule.selectorText);
      if (!selector) continue;
      const decls = sanitizeStyleDeclaration(styleRule.style, isDirectWrapperSelector(selector));
      if (decls) chunks.push(`${selector}{${decls}}`);
    } else if (rule.type === CSSRule.MEDIA_RULE) {
      const mediaRule = rule as CSSMediaRule;
      const cond = (mediaRule.conditionText || mediaRule.media.mediaText || "").trim();
      if (!cond) continue;
      if (stripDarkMedia && /prefers-color-scheme\s*:\s*dark/i.test(cond)) continue;
      const inner = serializeSanitizedRules(mediaRule.cssRules, stripDarkMedia);
      if (inner) chunks.push(`@media ${cond}{${inner}}`);
    } else if (rule.type === CSSRule.SUPPORTS_RULE) {
      const supportsRule = rule as CSSSupportsRule;
      const cond = supportsRule.conditionText.trim();
      if (!cond) continue;
      const inner = serializeSanitizedRules(supportsRule.cssRules, stripDarkMedia);
      if (inner) chunks.push(`@supports ${cond}{${inner}}`);
    }
  }
  return chunks.join("\n");
}

function sanitizeEmailStylesheet(rawCss: string, stripDarkMedia: boolean): string {
  const cleaned = rawCss.replace(/<!--|-->/g, "").trim();
  if (!cleaned) return "";
  const rules = parseCssRules(cleaned);
  if (!rules) return "";
  return serializeSanitizedRules(rules, stripDarkMedia);
}

/** 修复未闭合的 <title>/<style>（在 </head> 或 <body> 前缺失闭合标签时补齐），避免整封正文被吞进头部。 */
function closeUnclosedRawtextBeforeBody(html: string, tagName: "title" | "style"): string {
  const lower = html.toLowerCase();
  const openToken = `<${tagName}`;
  const closeToken = `</${tagName}`;
  let cursor = 0;
  let result = "";
  let changed = false;

  while (cursor < html.length) {
    const openIdx = lower.indexOf(openToken, cursor);
    if (openIdx === -1) break;
    const afterChar = lower[openIdx + openToken.length];
    if (afterChar && !/[\s>]/.test(afterChar)) {
      result += html.slice(cursor, openIdx + openToken.length);
      cursor = openIdx + openToken.length;
      continue;
    }
    const openEnd = lower.indexOf(">", openIdx + openToken.length);
    if (openEnd === -1) break;

    const closeIdx = lower.indexOf(closeToken, openEnd + 1);
    const headEndIdx = lower.indexOf("</head", openEnd + 1);
    const bodyStartIdx = lower.indexOf("<body", openEnd + 1);
    let boundary = -1;
    if (headEndIdx !== -1) boundary = headEndIdx;
    if (bodyStartIdx !== -1 && (boundary === -1 || bodyStartIdx < boundary)) {
      boundary = bodyStartIdx;
    }

    if (boundary !== -1 && (closeIdx === -1 || boundary < closeIdx)) {
      result += html.slice(cursor, boundary) + `</${tagName}>`;
      cursor = boundary;
      changed = true;
    } else if (closeIdx !== -1) {
      const closeEnd = lower.indexOf(">", closeIdx + closeToken.length);
      const nextPos = closeEnd === -1 ? html.length : closeEnd + 1;
      result += html.slice(cursor, nextPos);
      cursor = nextPos;
    } else {
      break;
    }
  }

  if (!changed) return html;
  return result + html.slice(cursor);
}

/** 预处理 XHTML 自闭合 rawtext 标签、未闭合头部标签与 noscript 块，防止 DOMParser 吞噬正文或泄漏回退文本。 */
function normalizeRawEmailHtml(raw: string): string {
  let html = raw.replace(/^\uFEFF?\s*<\?xml\b[^>]*\?>/i, "");
  html = html.replace(
    /<(title|style|script|textarea|noscript|iframe|noembed|noframes|xmp)(\s[^<>]*?)?\s*\/>/gi,
    "<$1$2></$1>",
  );
  html = html.replace(/<noscript\b[^>]*>[\s\S]*?<\/noscript\s*>/gi, "");
  html = closeUnclosedRawtextBeforeBody(html, "title");
  html = closeUnclosedRawtextBeforeBody(html, "style");
  return html;
}

function getMarkdownSanitizer(): ReturnType<typeof DOMPurify> {
  if (!markdownSanitizer) {
    const instance = DOMPurify(window);
    instance.addHook("afterSanitizeAttributes", (node) => {
      if (!(node instanceof Element)) return;
      const tag = node.tagName.toLowerCase();
      if (tag === "a") {
        const href = node.getAttribute("href");
        if (href && !isSafeLinkUrl(href)) {
          node.removeAttribute("href");
        }
        node.setAttribute("target", "_blank");
        node.setAttribute("rel", "noopener noreferrer nofollow");
      } else if (tag === "img") {
        const src = node.getAttribute("src");
        if (src && !isSafeImageUrl(src)) {
          node.removeAttribute("src");
        }
        node.setAttribute("referrerpolicy", "no-referrer");
        node.setAttribute("loading", "lazy");
      }
    });
    markdownSanitizer = instance;
  }
  return markdownSanitizer;
}

function getHtmlSanitizer(): ReturnType<typeof DOMPurify> {
  if (!htmlSanitizer) {
    const instance = DOMPurify(window);
    instance.addHook("afterSanitizeAttributes", (node) => {
      if (!(node instanceof Element)) return;
      const tag = node.tagName.toLowerCase();
      if (tag === "a") {
        const href = node.getAttribute("href");
        if (href && !isSafeLinkUrl(href)) {
          node.removeAttribute("href");
        }
        node.setAttribute("target", "_blank");
        node.setAttribute("rel", "noopener noreferrer nofollow");
      } else if (tag === "img" || tag === "source") {
        const src = node.getAttribute("src");
        if (src && !isSafeImageUrl(src)) {
          node.removeAttribute("src");
        }
        if (tag === "img") {
          node.setAttribute("referrerpolicy", "no-referrer");
          node.setAttribute("loading", "lazy");
        }
      }
      const bg = node.getAttribute("background");
      if (bg !== null && !isSafeImageUrl(bg)) {
        node.removeAttribute("background");
      }
      const rawStyle = node.getAttribute("style");
      if (rawStyle !== null) {
        const cleanStyle = sanitizeInlineStyleText(rawStyle, false);
        if (cleanStyle) node.setAttribute("style", cleanStyle);
        else node.removeAttribute("style");
      }
    });
    htmlSanitizer = instance;
  }
  return htmlSanitizer;
}

export function normalizeBodyFormat(raw: string): NormalizedBodyFormat {
  const upper = (raw || "").trim().toUpperCase();
  if (upper === "HTML") return "HTML";
  if (upper === "MARKDOWN") return "MARKDOWN";
  return "PLAIN";
}

export function renderMarkdownBody(raw: string): string {
  const rendered = markdown.render(raw);
  return getMarkdownSanitizer().sanitize(rendered, {
    ALLOWED_TAGS: MARKDOWN_ALLOWED_TAGS,
    ALLOWED_ATTR: MARKDOWN_ALLOWED_ATTR,
    ALLOW_DATA_ATTR: false,
  });
}

function detectTemplateStyling(raw: string, body: HTMLElement, css: string): boolean {
  if (css.length > 0) return true;
  if (
    body.hasAttribute("bgcolor") ||
    body.hasAttribute("background") ||
    body.hasAttribute("text")
  ) {
    return true;
  }
  const bodyStyle = body.getAttribute("style") || "";
  if (/(?:background|color)\s*:/i.test(bodyStyle)) return true;
  if (
    body.querySelector("[bgcolor], [background], table[width], table[role='presentation'], svg")
  ) {
    return true;
  }
  const styledEls = body.querySelectorAll("[style]");
  for (let i = 0; i < styledEls.length; i += 1) {
    const val = styledEls[i]?.getAttribute("style") || "";
    if (/(?:^|;)\s*(?:background|background-color|background-image|color)\s*:/i.test(val)) {
      return true;
    }
  }
  return /<!doctype\s+html\b/i.test(raw) && body.querySelector("table") !== null;
}

function extractSafeBodyAttrs(body: HTMLElement): PreparedHtmlEmail["bodyAttrs"] {
  const baseDecls: string[] = [];
  const bgColor = normalizeLegacyColor(body.getAttribute("bgcolor"));
  if (bgColor) baseDecls.push(`background-color:${bgColor}`);
  const textColor = normalizeLegacyColor(body.getAttribute("text"));
  if (textColor) baseDecls.push(`color:${textColor}`);
  const bgImg = body.getAttribute("background");
  if (bgImg && isSafeImageUrl(bgImg)) {
    baseDecls.push(`background-image:url("${bgImg.trim().replace(/"/g, '\\"')}")`);
  }
  const inlineClean = sanitizeInlineStyleText(body.getAttribute("style") || "", true);
  if (inlineClean) baseDecls.push(inlineClean);

  const className = (body.getAttribute("class") || "")
    .split(/\s+/)
    .filter((token) => SAFE_CLASS_TOKEN_RE.test(token))
    .join(" ");
  const rawDir = (body.getAttribute("dir") || "").trim().toLowerCase();
  const dir = rawDir === "ltr" || rawDir === "rtl" || rawDir === "auto" ? rawDir : null;
  const rawLang = (body.getAttribute("lang") || "").trim();
  const lang = SAFE_LANG_RE.test(rawLang) ? rawLang : null;

  return {
    className,
    style: baseDecls.join(";"),
    dir,
    lang,
  };
}

export function prepareHtmlEmail(raw: string): PreparedHtmlEmail {
  const normalized = normalizeRawEmailHtml(raw);
  const doc = new DOMParser().parseFromString(normalized, "text/html");
  const body = doc.body;
  if (!body) {
    return {
      bodyHtml: "",
      css: "",
      hasTemplateStyling: false,
      bodyAttrs: { className: "", style: "", dir: null, lang: null },
    };
  }

  // 逐个提取 <head> 和 <body> 中的样式表文本并独立清理，避免单个损坏的 <style> 影响后续样式块。
  const styleNodes = Array.from(doc.querySelectorAll("style"));
  const rawStyleBlocks = styleNodes.map((el) => el.textContent || "");
  for (const el of styleNodes) el.remove();

  // 移除正文中的 HTML 注释（含 MSO 条件注释），避免 DOMPurify SAFE_FOR_XML 误删同节点含 &lt; 的文本。
  const walker = doc.createTreeWalker(body, NodeFilter.SHOW_COMMENT);
  const comments: Comment[] = [];
  while (walker.nextNode()) comments.push(walker.currentNode as Comment);
  for (const comment of comments) comment.remove();

  const bodyAttrs = extractSafeBodyAttrs(body);
  const bodyHtml = getHtmlSanitizer().sanitize(body.innerHTML, {
    USE_PROFILES: { html: true, svg: true },
    ADD_ATTR: HTML_ADD_ATTR,
    FORBID_TAGS: HTML_FORBID_TAGS,
    FORBID_ATTR: HTML_FORBID_ATTR,
    ALLOW_DATA_ATTR: false,
  });

  const rawProvisionalCss = rawStyleBlocks
    .map((block) => sanitizeEmailStylesheet(block, false))
    .filter(Boolean)
    .join("\n");
  const hasTemplateStyling = detectTemplateStyling(raw, body, rawProvisionalCss);
  // 邮件正文画布在浅色模式下为白底、深色模式下带模板样式也使用亮底画布，移除 dark 媒体查询避免白字落到白底。
  const css = rawStyleBlocks
    .map((block) => sanitizeEmailStylesheet(block, true))
    .filter(Boolean)
    .join("\n");

  return {
    bodyHtml,
    css,
    hasTemplateStyling,
    bodyAttrs,
  };
}

export const SHADOW_BASE_CSS = `
:host {
  display: block;
  width: 100%;
  max-width: 100%;
  min-width: 0;
  contain: layout style;
  isolation: isolate;
}
:where(:host > .agently-mail-root) {
  box-sizing: border-box;
  display: block;
  width: 100%;
  max-width: 100%;
  min-width: 0;
  overflow-x: auto;
  overflow-y: visible;
  overflow-wrap: break-word;
  font-family: inherit;
  font-size: 15px;
  line-height: 1.75;
  color: inherit;
}
:where(:host > .agently-mail-root.mail-paper) {
  background: #ffffff;
  color: #202934;
  color-scheme: light;
  border-radius: 10px;
  padding: 16px;
  border: 1px solid var(--line, #293342);
}
:where(:host > .agently-mail-root > .agently-mail-body) {
  margin: 0;
  padding: 0;
  min-width: 0;
}
:where(img, svg) {
  max-width: 100%;
  height: auto;
}
:where(a) {
  color: var(--accent, #176fd1);
}
:where(.mail-paper a) {
  color: #176fd1;
}
:where(pre) {
  overflow-x: auto;
  padding: 12px 16px;
  background: var(--surface-subtle, #f7f9fb);
  border-radius: 8px;
}
:where(.mail-paper pre) {
  background: #f7f9fb;
}
:where(code) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.9em;
}
:where(blockquote) {
  margin: 12px 0;
  padding: 8px 14px;
  border-left: 3px solid var(--line-strong, #cbd4df);
}
:where(.mail-paper blockquote) {
  border-left-color: #cbd4df;
}
:where(table) {
  border-collapse: collapse;
}
`;

export const SHADOW_GUARD_CSS = `
:host > .agently-mail-root {
  position: relative !important;
  display: block !important;
  visibility: visible !important;
  opacity: 1 !important;
  width: 100% !important;
  max-width: 100% !important;
  height: auto !important;
  min-height: 0 !important;
  max-height: none !important;
  overflow-x: auto !important;
  overflow-y: visible !important;
}
:host > .agently-mail-root > .agently-mail-body {
  position: relative !important;
  visibility: visible !important;
  opacity: 1 !important;
  height: auto !important;
  max-height: none !important;
  overflow-y: visible !important;
}
`;
