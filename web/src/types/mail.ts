/** 与 docs/web-api.md 对齐的真实 Web API 类型。 */

export interface Sender {
  email: string;
  name?: string | null;
}

/** 读信全文里的附件元数据；content 与 download_url 不出后端。 */
export interface Attachment {
  attachment_id?: string | null;
  filename?: string | null;
  content_type?: string | null;
  size?: number | null;
}

export interface MessageSummary {
  message_id: string;
  subject?: string | null;
  from_?: Sender | null;
  created_at?: string | null;
  is_read?: boolean | null;
}

export interface Message {
  message_id: string;
  subject?: string | null;
  body: string;
  body_format: string;
  from_?: Sender | null;
  to: Sender[];
  cc: Sender[];
  bcc: Sender[];
  attachments: Attachment[];
  created_at?: string | null;
}

export interface Alias {
  alias_id: string;
  email?: string | null;
  is_primary: boolean;
}

export type AccountStatus = "ok" | "invalid_credentials" | "auth_expired" | "error";

export interface AccountView {
  account: string;
  status: AccountStatus;
  app_id?: string | null;
  expires_at?: string | null;
  email?: string | null;
  aliases: Alias[] | null;
  error?: string | null;
}

export interface AccountInfo {
  nickname?: string | null;
  agent_alias_limit?: number | null;
}

/** me 只供额度与账号信息；别名以 accounts 为唯一数据源。 */
export interface Me {
  account: string;
  aliases: Alias[];
  rate_limits: Record<string, unknown>;
  constraints: Record<string, unknown>;
  scopes: string[];
  credential: { app_id: string; expires_at: string; expired: boolean };
  sent_today_local: number;
  account_info: AccountInfo | null;
}

export interface Page<T> {
  data: T[];
  has_more: boolean;
  next_cursor?: string | null;
}

/** 发送内联附件：content 为不带 data URL 前缀的纯 Base64。 */
export interface OutboundAttachment {
  filename: string;
  content_type?: string;
  content: string;
}

export interface SendResult {
  queued: true;
  to: string[];
  subject: string;
  attachment_count: number;
  local_count_recorded: boolean;
}

export interface ReplyResult {
  queued: true;
  message_id: string;
  reply_all: boolean;
  local_count_recorded: boolean;
}

export interface ForwardResult {
  queued: true;
  message_id: string;
  to: string[];
  include_attachments: boolean;
  local_count_recorded: boolean;
}

export interface TrashResult {
  message_id: string;
  trashed: true;
}

export interface CreateAddressResult {
  alias: Alias;
  reused: boolean;
  aliases: Alias[] | null;
  refresh_required: boolean;
  refresh_error: string | null;
}

export interface DeleteAddressResult {
  alias_id: string;
  aliases: Alias[] | null;
  refresh_required: boolean;
  refresh_error: string | null;
}

export type Tone = "blue" | "green" | "orange" | "slate";
export type FolderId = "inbox" | "sent" | "spam" | "trash";
export type MailFilter = "all" | "unread" | "attachment";
export type BodyFormat = "PLAIN" | "MARKDOWN" | "HTML";

/** 账号展示装饰，由 account/email 本地派生，不来自服务端。 */
export interface DisplayAccount extends AccountView {
  aliases: Alias[];
  name: string;
  initials: string;
  tone: Tone;
}

export interface QuotaView {
  dailySend: string;
  perMinute: string;
  perHour: string;
  singleAttachment: string;
  totalAttachment: string;
}

export type LoginState =
  | "waiting_scan"
  | "scanned"
  | "select_mail"
  | "authorizing"
  | "success"
  | "expired"
  | "cancelled"
  | "error";

export interface LoginMailOption {
  email: string;
  is_selected: boolean;
}

export interface WebLoginSession {
  session_id: string;
  account: string;
  mode: "qr" | "browser";
  state: LoginState;
  qr_image: string | null;
  qr_expired: boolean;
  browser_url: string;
  input_code: string;
  expires_at: string;
  mails: LoginMailOption[] | null;
  email: string | null;
  error: string | null;
  error_code: string | null;
}

export interface LoginCancelResult {
  cancelled: boolean;
  session: WebLoginSession | null;
}
