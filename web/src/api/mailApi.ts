/** 真实 Web API 客户端：每个方法对应 docs/web-api.md 的一个端点。 */

import type { AstrBotBridge } from "./bridge";
import type {
  AccountView,
  BodyFormat,
  CreateAddressResult,
  DeleteAddressResult,
  FolderId,
  ForwardResult,
  LoginCancelResult,
  Me,
  Message,
  MessageSummary,
  OutboundAttachment,
  Page,
  ReplyResult,
  SendResult,
  TrashResult,
  WebLoginSession,
} from "../types/mail";

export const ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024;
export const ATTACHMENT_MAX_COUNT = 50;
// 实测默认别名上限；真实值优先取 me.account_info.agent_alias_limit。
export const ALIAS_LIMIT_FALLBACK = 5;

export interface ListMessagesParams {
  account: string;
  alias_id: string;
  dir: FolderId;
  limit?: number;
  cursor?: string;
  unread?: boolean;
}

export interface SearchParams {
  account: string;
  alias_id: string;
  q: string;
  search_in?: string;
  limit?: number;
  cursor?: string;
}

export interface MessageRef {
  account: string;
  alias_id: string;
  message_id: string;
}

export interface AttachmentRef extends MessageRef {
  attachment_id?: string;
  index?: number;
}

export interface SendParams {
  account: string;
  alias_id: string;
  to: string[];
  cc?: string[];
  bcc?: string[];
  subject: string;
  body: string;
  body_format: BodyFormat;
  attachments?: OutboundAttachment[];
}

export interface ReplyParams extends MessageRef {
  body: string;
  body_format: BodyFormat;
  reply_all?: boolean;
}

export interface ForwardParams extends MessageRef {
  to: string[];
  body: string;
  body_format: BodyFormat;
  include_attachments?: boolean;
}

export interface MailApi {
  listAccounts(): Promise<AccountView[]>;
  me(account: string): Promise<Me>;
  listMessages(params: ListMessagesParams): Promise<Page<MessageSummary>>;
  search(params: SearchParams): Promise<Page<MessageSummary>>;
  message(params: MessageRef): Promise<Message>;
  downloadAttachment(params: AttachmentRef, filename?: string): Promise<{ filename: string }>;
  trash(params: MessageRef): Promise<TrashResult>;
  send(params: SendParams): Promise<SendResult>;
  reply(params: ReplyParams): Promise<ReplyResult>;
  forward(params: ForwardParams): Promise<ForwardResult>;
  createAddress(account: string): Promise<CreateAddressResult>;
  deleteAddress(account: string, aliasId: string): Promise<DeleteAddressResult>;
  loginStart(account: string): Promise<WebLoginSession>;
  loginStatus(sessionId: string): Promise<WebLoginSession>;
  loginSelect(sessionId: string, email: string): Promise<WebLoginSession>;
  loginCancel(sessionId?: string): Promise<LoginCancelResult>;
}

export function createMailApi(bridge: AstrBotBridge): MailApi {
  return {
    async listAccounts() {
      const result = await bridge.apiGet<{ accounts: AccountView[] }>("accounts");
      return result.accounts;
    },
    me(account) {
      return bridge.apiGet<Me>("me", { account });
    },
    listMessages(params) {
      return bridge.apiGet<Page<MessageSummary>>("messages", { ...params });
    },
    search(params) {
      return bridge.apiGet<Page<MessageSummary>>("search", {
        search_in: "SEARCH_IN_ALL",
        ...params,
      });
    },
    message(params) {
      return bridge.apiGet<Message>("message", { ...params });
    },
    downloadAttachment(params, filename) {
      return bridge.download("attachment", { ...params }, filename);
    },
    trash(params) {
      return bridge.apiPost<TrashResult>("trash", { ...params });
    },
    send(params) {
      return bridge.apiPost<SendResult>("send", { ...params });
    },
    reply(params) {
      return bridge.apiPost<ReplyResult>("reply", { ...params });
    },
    forward(params) {
      return bridge.apiPost<ForwardResult>("forward", { ...params });
    },
    createAddress(account) {
      return bridge.apiPost<CreateAddressResult>("create_address", { account });
    },
    deleteAddress(account, aliasId) {
      return bridge.apiPost<DeleteAddressResult>("delete_address", {
        account,
        alias_id: aliasId,
      });
    },
    loginStart(account) {
      return bridge.apiPost<WebLoginSession>("login_start", { account });
    },
    loginStatus(sessionId) {
      return bridge.apiGet<WebLoginSession>("login_status", { session_id: sessionId });
    },
    loginSelect(sessionId, email) {
      return bridge.apiPost<WebLoginSession>("login_select", {
        session_id: sessionId,
        email,
      });
    },
    loginCancel(sessionId) {
      return bridge.apiPost<LoginCancelResult>(
        "login_cancel",
        sessionId ? { session_id: sessionId } : {},
      );
    },
  };
}
