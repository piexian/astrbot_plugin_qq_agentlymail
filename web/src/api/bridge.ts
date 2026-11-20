/** AstrBotPluginView bridge 的最小类型与访问入口；不接触任何凭证。 */

export interface BridgeContext {
  pluginName?: string;
  displayName?: string;
  pageName?: string;
  pageTitle?: string;
  locale?: string;
  i18n?: Record<string, unknown>;
  isDark?: boolean;
  themeColors?: { primary?: string; secondary?: string };
}

export interface AstrBotBridge {
  ready(): Promise<BridgeContext>;
  getContext(): BridgeContext | null;
  getLocale?(): string;
  getI18n?(): Record<string, unknown>;
  t?(key: string, fallback?: string): string;
  onContext?(handler: (context: BridgeContext) => void): () => void;
  apiGet<T>(endpoint: string, params?: Record<string, unknown>): Promise<T>;
  apiPost<T>(endpoint: string, body?: Record<string, unknown>): Promise<T>;
  download(
    endpoint: string,
    params?: Record<string, unknown>,
    filename?: string,
  ): Promise<{ filename: string }>;
}

declare global {
  interface Window {
    AstrBotPluginView?: AstrBotBridge;
    AstrBotPluginPage?: AstrBotBridge;
  }
}

export function getBridge(): AstrBotBridge | null {
  return window.AstrBotPluginView ?? window.AstrBotPluginPage ?? null;
}
