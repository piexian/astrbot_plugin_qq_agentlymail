/** 分页摘要列表与服务端搜索；筛选键独立维护 cursor，令牌守卫丢弃旧响应。 */

import { computed, onScopeDispose, shallowRef, watch } from "vue";
import type { MailApi } from "../api/mailApi";
import type { FolderId, MailFilter, MessageSummary, Page } from "../types/mail";
import { errorMessage } from "../utils/error";

const PAGE_LIMIT = 20;
const DEBOUNCE_MS = 300;

export interface MessageListDeps {
  api: MailApi;
  account: () => string | null;
  alias: () => string | null;
}

function dedupe(existing: MessageSummary[], incoming: MessageSummary[]): MessageSummary[] {
  const seen = new Set(existing.map((item) => item.message_id));
  const added = incoming.filter((item) => !seen.has(item.message_id));
  return [...existing, ...added];
}

export function useMessageList(deps: MessageListDeps) {
  const folder = shallowRef<FolderId>("inbox");
  const filter = shallowRef<MailFilter>("all");
  const query = shallowRef("");
  const debouncedQuery = shallowRef("");

  const items = shallowRef<MessageSummary[]>([]);
  const hasMore = shallowRef(false);
  const nextCursor = shallowRef<string | null>(null);
  const loading = shallowRef(false);
  const loadingMore = shallowRef(false);
  const error = shallowRef<string | null>(null);

  let activeToken = 0;
  let debounceTimer: number | undefined;

  const searchMode = computed(() => debouncedQuery.value !== "");

  const filterKey = computed(() => {
    const account = deps.account();
    const alias = deps.alias();
    if (!account || !alias) return null;
    if (searchMode.value) return `s:${account}:${alias}:${debouncedQuery.value}`;
    return `m:${account}:${alias}:${folder.value}:${filter.value === "unread" ? "u" : "a"}`;
  });

  watch(query, (value) => {
    if (debounceTimer) window.clearTimeout(debounceTimer);
    debounceTimer = window.setTimeout(() => {
      debouncedQuery.value = value.trim();
    }, DEBOUNCE_MS);
  });

  onScopeDispose(() => {
    if (debounceTimer) window.clearTimeout(debounceTimer);
  });

  function requestPage(
    cursor: string | null,
    account: string,
    alias: string,
  ): Promise<Page<MessageSummary>> {
    if (searchMode.value) {
      return deps.api.search({
        account,
        alias_id: alias,
        q: debouncedQuery.value,
        limit: PAGE_LIMIT,
        cursor: cursor ?? undefined,
      });
    }
    return deps.api.listMessages({
      account,
      alias_id: alias,
      dir: folder.value,
      limit: PAGE_LIMIT,
      cursor: cursor ?? undefined,
      unread: filter.value === "unread" ? true : undefined,
    });
  }

  function clearList() {
    activeToken += 1;
    items.value = [];
    hasMore.value = false;
    nextCursor.value = null;
    error.value = null;
    loading.value = false;
    loadingMore.value = false;
  }

  async function loadFirstPage(): Promise<void> {
    const account = deps.account();
    const alias = deps.alias();
    if (!account || !alias) return;
    const token = ++activeToken;
    loading.value = true;
    loadingMore.value = false;
    error.value = null;
    items.value = [];
    hasMore.value = false;
    nextCursor.value = null;
    try {
      const page = await requestPage(null, account, alias);
      if (token !== activeToken) return;
      items.value = dedupe([], page.data);
      hasMore.value = page.has_more;
      nextCursor.value = page.next_cursor ?? null;
    } catch (cause) {
      if (token !== activeToken) return;
      error.value = errorMessage(cause);
    } finally {
      if (token === activeToken) loading.value = false;
    }
  }

  watch(
    filterKey,
    async (key) => {
      if (!key) {
        clearList();
        return;
      }
      await loadFirstPage();
      // 同步切换可能合并依赖变化，完成后按回调的键核对，不一致就补拉当前列表。
      if (filterKey.value !== key) await loadFirstPage();
    },
    { immediate: true },
  );

  async function loadMore() {
    if (loading.value || loadingMore.value || !hasMore.value || !nextCursor.value) return;
    const account = deps.account();
    const alias = deps.alias();
    const key = filterKey.value;
    if (!key || !account || !alias) return;
    const token = activeToken;
    const cursor = nextCursor.value;
    loadingMore.value = true;
    try {
      const page = await requestPage(cursor, account, alias);
      if (token !== activeToken || key !== filterKey.value) return;
      items.value = dedupe(items.value, page.data);
      hasMore.value = page.has_more;
      nextCursor.value = page.next_cursor ?? null;
    } catch (cause) {
      if (token !== activeToken || key !== filterKey.value) return;
      error.value = errorMessage(cause);
    } finally {
      if (token === activeToken) loadingMore.value = false;
    }
  }

  function reload() {
    if (filterKey.value) void loadFirstPage();
  }

  function clearQuery() {
    if (debounceTimer) window.clearTimeout(debounceTimer);
    query.value = "";
    debouncedQuery.value = "";
  }

  function resetView() {
    folder.value = "inbox";
    filter.value = "all";
    clearQuery();
  }

  function setFolder(next: FolderId) {
    clearQuery();
    folder.value = next;
  }

  function setFilter(next: MailFilter) {
    if (next === "attachment") return;
    if (searchMode.value && next !== "all") return;
    filter.value = next;
  }

  function setQuery(next: string) {
    query.value = next;
  }

  return {
    folder,
    filter,
    query,
    searchMode,
    items,
    hasMore,
    loading,
    loadingMore,
    error,
    setFolder,
    setFilter,
    setQuery,
    loadMore,
    reload,
    resetView,
  };
}

export type MessageList = ReturnType<typeof useMessageList>;
