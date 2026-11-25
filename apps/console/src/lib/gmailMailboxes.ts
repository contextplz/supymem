export type GmailMailbox = {
  id: string;
  email: string;
  status: string;
  is_default: boolean;
  sent_today: number;
  daily_send_limit: number;
  needs_reconnect: boolean;
  updated_at: string;
};

export type GmailMailboxes = {
  multi_gmail_v1: true;
  workspace_id: string;
  mailboxes: GmailMailbox[];
  venture_id?: string;
  venture_default_mailbox_id?: string | null;
  workspace_default_mailbox_id?: string | null;
};

export function checkedGmailMailboxes(value: unknown, workspaceId: string, ventureId?: string): GmailMailboxes | null {
  const data = value as Partial<GmailMailboxes> | null;
  if (data?.multi_gmail_v1 !== true) return null;
  if (data.workspace_id !== workspaceId || !Array.isArray(data.mailboxes) ||
      (ventureId && data.venture_id !== ventureId) ||
      data.mailboxes.some(mailbox => !mailbox || typeof mailbox.id !== "string" || !mailbox.id ||
        typeof mailbox.email !== "string" || !mailbox.email || typeof mailbox.status !== "string" ||
        typeof mailbox.is_default !== "boolean" || typeof mailbox.needs_reconnect !== "boolean" ||
        !Number.isFinite(mailbox.sent_today) || mailbox.sent_today < 0 || !Number.isFinite(mailbox.daily_send_limit) || mailbox.daily_send_limit < 0 ||
        typeof mailbox.updated_at !== "string" || !Number.isFinite(Date.parse(mailbox.updated_at))) ||
      new Set(data.mailboxes.map(mailbox => mailbox.id)).size !== data.mailboxes.length) {
    throw new Error("Your Gmail inboxes could not be verified for this workspace. Refresh and try again.");
  }
  return data as GmailMailboxes;
}

export const mailboxCanSend = (mailbox: GmailMailbox) => mailbox.status === "active" && !mailbox.needs_reconnect;
