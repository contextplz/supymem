"""Your local mailbox settings and standard-library SMTP/IMAP transport.

Save your SMTP server, port (587 with STARTTLS or 465 with TLS), mailbox address,
app password, sender name and postal address in Keys and connections > Email.
Sending refuses a missing postal address.
shortcut: SMTP usernames must be mailbox addresses, add a separate From setting
when supporting servers with non-address logins.

For Gmail, enable two-step verification in your Google account, create an app
password under Security > App passwords, and use it instead of your account
password. Use Google's documented SMTP and IMAP settings. If App passwords is
unavailable, ask your account administrator or use a mailbox that supports it.
Do not put your password in source control. Composio is not required.

Save your IMAP server and port (993) on the same screen. IMAP uses TLS,
the same username/password, and a read-only INBOX. Replies flow through the
existing reply classifier and suppression handling. Without IMAP configured,
you must honor opt-out replies in your mailbox before sending again.

Your footer uses reply-based opt-out by default. Public instances use the app's
private owner-session signing material for links to /public/unsubscribe.
"""

from __future__ import annotations

import base64
import imaplib
import os
import re
import smtplib
import ssl
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr, make_msgid, parsedate_to_datetime
from urllib.parse import urlsplit

from sqlalchemy import select

from app.core.local_mode import is_local

try:
    from app.core.local_capabilities import enabled, blocked_message
except ImportError:
    # shortcut: owner switches arrive with the shared capability module, use saved settings until it is merged.
    def enabled(name: str) -> bool:
        if name == "connected_apps":
            from app.core.local_secrets import get_secret, read_settings
            return bool(get_secret("composio", "api_key") and read_settings("composio")["values"].get("auth_configs"))
        try:
            config = settings()
        except ValueError:
            return False
        return bool(config.imap_host) if name == "read_replies" else name == "send_email" and bool(config.sender_name and config.postal_address)

    def blocked_message(name: str) -> str:
        if name == "read_replies":
            return "Open Keys and connections > Email, add your IMAP server and port, and turn on Read replies, then ask me to check replies again."
        return "Open Keys and connections > Email, add your SMTP server and port, mailbox address, app password, sender name and postal address, and turn on Send email, then try again."


@dataclass(frozen=True, repr=False)
class Settings:
    host: str
    port: int
    username: str
    password: str
    sender_name: str
    postal_address: str
    imap_host: str
    imap_port: int
    booking_link: str = ""


def _address(value: str) -> str:
    if not value or len(value) > 320 or any(character in value for character in "\r\n"):
        raise ValueError("Use one valid mailbox address, then try again.")
    parsed = Address(addr_spec=value)
    if not parsed.username or not parsed.domain:
        raise ValueError("Use your full mailbox address, then try again.")
    return value


def _values() -> dict:
    from app.core.local_secrets import get_secret, read_settings

    saved = read_settings("smtp")["values"]
    fields = {"SMTP_HOST": "host", "SMTP_PORT": "port", "SMTP_USERNAME": "user",
        "SENDER_NAME": "sender_name", "SENDER_POSTAL_ADDRESS": "postal_address",
        "IMAP_HOST": "imap_host", "IMAP_PORT": "imap_port", "BOOKING_LINK": "booking_link"}
    defaults = {"SMTP_PORT": 587, "IMAP_PORT": 993}
    values = {key: saved.get(field, defaults.get(key, ""))
        for key, field in fields.items()}
    for key in ("IMAP_HOST", "BOOKING_LINK"):
        if values[key] is None:
            values[key] = ""
    values["SMTP_PASSWORD"] = get_secret("smtp", "password") or ""
    return {key: value.strip() if isinstance(value, str) else value for key, value in values.items()}


def configured() -> bool:
    if not is_local():
        return False
    values = _values()
    return all(isinstance(values[key], str) and values[key] for key in (
        "SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD"))


def settings() -> Settings:
    if not is_local():
        raise ValueError("Your local mailbox is disabled. Enable local mode before configuring it.")
    if not configured():
        raise ValueError(blocked_message("send_email"))
    values = _values()
    smtp_port = values.pop("SMTP_PORT")
    imap_port = values.pop("IMAP_PORT")
    if any(not isinstance(value, str) or "\r" in value or "\n" in value for key, value in values.items()
            if key not in {"SMTP_PASSWORD", "SENDER_POSTAL_ADDRESS"}):
        raise ValueError("Open Keys and connections > Email and keep each mailbox setting on one line, then try again.")
    try:
        port = int(smtp_port)
        imap_port = int(imap_port)
        if not 1 <= port <= 65535 or not 1 <= imap_port <= 65535:
            raise ValueError
        username = _address(values["SMTP_USERNAME"])
    except (TypeError, ValueError, HeaderParseError):
        raise ValueError("Open Keys and connections > Email and check your mailbox address and mail server ports, then try again.") from None
    imap_host = values["IMAP_HOST"] or {"smtp.gmail.com": "imap.gmail.com",
        "smtp.office365.com": "outlook.office365.com"}.get(values["SMTP_HOST"].lower(), "")
    if values["BOOKING_LINK"]:
        from app.gtm.contact_work import valid_booking_link
        try:
            values["BOOKING_LINK"] = valid_booking_link(values["BOOKING_LINK"])
        except ValueError:
            raise ValueError("Open Keys and connections > Email and enter a valid HTTPS booking link, then try again.") from None
    return Settings(values["SMTP_HOST"], port, username, values["SMTP_PASSWORD"],
        values["SENDER_NAME"], values["SENDER_POSTAL_ADDRESS"], imap_host, imap_port, values["BOOKING_LINK"])


def public_url() -> str:
    value = os.environ.get("SUPYMEM_PUBLIC_URL", "").strip().rstrip("/")
    if value:
        try:
            parsed = urlsplit(value)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or any(character.isspace() for character in value)
                    or parsed.port is not None and not 1 <= parsed.port <= 65535):
                raise ValueError
        except ValueError:
            raise ValueError("Set your public URL to an HTTP or HTTPS address without credentials, then try again.") from None
    return value


def ensure_account(session, workspace_id):
    from core.clock import utcnow
    from core.ids import new_id

    from app.connections.mailboxes import MailboxUnavailable
    from app.db.models import GtmEmailAccount, Workspace
    from app.gtm.supymem.composio_gmail import user_id_for
    from app.gtm.supymem.pipeline import _gmail_workspace_ids
    from app.tenancy import current_or_none

    if not is_local():
        return None
    try:
        config = settings()
    except ValueError as error:
        raise MailboxUnavailable(str(error)) from None
    workspace = session.get(Workspace, workspace_id)
    scope = current_or_none()
    if (workspace is None or not workspace.owner_id or (scope and scope.workspace_id != "none"
            and workspace_id not in _gmail_workspace_ids(session, scope.workspace_id))):
        raise MailboxUnavailable("Open your own workspace before configuring your mailbox.")
    accounts = list(session.scalars(select(GtmEmailAccount).where(GtmEmailAccount.provider == "smtp")))
    for account in accounts:
        owner = session.get(Workspace, account.workspace_id)
        if owner is None or owner.owner_id != workspace.owner_id:
            raise MailboxUnavailable("Your mailbox belongs to another workspace owner. Open your own local instance.")
    for account in accounts:
        if account.workspace_id == workspace_id and account.email == config.username:
            if account.status != "active":
                raise MailboxUnavailable("Your mailbox is paused. Check your mailbox settings before reconnecting.")
            account.composio_user_id = account.app_user_id = user_id_for(workspace_id)
            session.flush()
            return account
    now = utcnow()
    account = GtmEmailAccount(id=new_id("gtmea"), workspace_id=workspace_id, provider="smtp",
        email=config.username, connected_account_id=new_id("smtp"), composio_user_id=user_id_for(workspace_id),
        app_user_id=user_id_for(workspace_id),
        status="active", created_at=now, updated_at=now)
    session.add(account)
    session.flush()
    return account


def check_binding(user_id: str, account_id: str) -> None:
    from app.db.models import GtmEmailAccount
    from app.gtm.supymem.composio_gmail import DefiniteFailure, user_id_for
    from app.state import db
    config = settings()
    with db()() as session:
        account = session.scalar(select(GtmEmailAccount).where(GtmEmailAccount.provider == "smtp",
            GtmEmailAccount.connected_account_id == account_id, GtmEmailAccount.composio_user_id == user_id,
            GtmEmailAccount.status == "active"))
        if account is None or account.email != config.username or user_id_for(account.workspace_id) != user_id:
            raise DefiniteFailure("Your mailbox settings changed. Select your current mailbox before sending or reading.")
        from app.gtm.supymem.pipeline import _gmail_workspace_ids
        from app.tenancy import current_or_none
        scope = current_or_none()
        if scope and scope.workspace_id != "none" and account.workspace_id not in _gmail_workspace_ids(session, scope.workspace_id):
            raise DefiniteFailure("Open the workspace that owns your mailbox before sending or reading.")


@contextmanager
def _smtp(config):
    from app.gtm.supymem.composio_gmail import DefiniteFailure
    from app.core.local_mode import is_test_mode
    if is_test_mode():
        raise DefiniteFailure("Sample mode never sends email. Review your saved draft instead.")
    transport = smtplib.SMTP_SSL if config.port == 465 else smtplib.SMTP
    options = {"timeout": 45}
    if config.port == 465:
        options["context"] = ssl.create_default_context()
    try:
        with transport(config.host, config.port, **options) as client:
            if config.port != 465:
                client.starttls(context=ssl.create_default_context())
            client.login(config.username, config.password)
            yield client
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
            smtplib.SMTPDataError, smtplib.SMTPNotSupportedError):
        raise DefiniteFailure("Your mailbox refused the email. Open Keys and connections > Email and check your app password, then check the recipient in Find customers and try again.") from None
    except (OSError, smtplib.SMTPException):
        raise RuntimeError("Your email delivery is unconfirmed. Check your Sent mailbox before trying again.") from None


def verify() -> dict:
    config = settings()
    with _smtp(config):
        pass
    return {"email": config.username, "successful": True, "log_id": None}


def send(*, to: str, subject: str, body: str, thread_id: str | None = None, self_only: bool = False) -> dict:
    from app.gtm.supymem.composio_gmail import DefiniteFailure
    if not enabled("send_email"):
        raise DefiniteFailure(blocked_message("send_email"))
    config = settings()
    if not config.postal_address or not config.sender_name:
        raise DefiniteFailure("Open Keys and connections > Email and add your sender name and business postal address, then try again. Your draft stays saved.")
    try:
        recipient = _address(to)
        if "\r" in subject or "\n" in subject:
            raise ValueError
        if thread_id and not re.fullmatch(r"<[^<>\s]+@[^<>\s]+>", thread_id):
            raise ValueError
        message = EmailMessage()
        message["From"] = formataddr((config.sender_name, config.username))
        message["To"] = recipient
        message["Subject"] = subject
        message["Date"] = datetime.now(timezone.utc)
        message["Message-ID"] = make_msgid(domain=Address(addr_spec=config.username).domain)
        if thread_id:
            message["In-Reply-To"] = thread_id
            message["References"] = thread_id
        message.set_content(body)
    except (ValueError, HeaderParseError):
        raise DefiniteFailure("Open Find customers and check the recipient address, subject and reply thread, then review your draft again.") from None
    if self_only and recipient != config.username:
        raise DefiniteFailure("Open Keys and connections > Email and use your current mailbox address for the self-send draft, then retry.")
    with _smtp(config) as client:
        refused = client.send_message(message, from_addr=config.username, to_addrs=[recipient])
        if refused:
            raise DefiniteFailure("Your mailbox refused the recipient. Open Find customers and check their address, then review your draft again.")
    receipt = str(message["Message-ID"])
    return {"provider_message_id": receipt, "rfc_message_id": receipt, "thread_id": thread_id or receipt}


@contextmanager
def _imap():
    from app.core.local_mode import is_test_mode
    if is_test_mode():
        raise ValueError("Sample mode never reads your mailbox. Review your sample contacts instead.")
    if not enabled("read_replies"):
        raise ValueError(blocked_message("read_replies"))
    config = settings()
    if not config.imap_host:
        raise ValueError("Open Keys and connections > Email and add your IMAP server and port, then ask me to check replies again.")
    try:
        with imaplib.IMAP4_SSL(config.imap_host, config.imap_port, ssl_context=ssl.create_default_context(), timeout=45) as client:
            client.login(config.username, config.password)
            status, _ = client.select("INBOX", readonly=True)
            if status != "OK":
                raise ValueError("Open your mailbox and check that your inbox is available, then try again.")
            yield client
    except (imaplib.IMAP4.error, OSError):
        raise RuntimeError("Your inbox could not be read. Open Keys and connections > Email and check your IMAP server, port and app password, then ask me to check replies again.") from None


def _checkpoint(client):
    validity = int(client.response("UIDVALIDITY")[1][0])
    last_uid = int(client.response("UIDNEXT")[1][0]) - 1
    return validity, last_uid


def _search(client, *criteria):
    status, data = client.uid("search", None, *criteria)
    if status != "OK":
        raise ValueError("Your inbox search failed. Check your mailbox connection, then try again.")
    return [int(value) for value in (data[0] or b"").split()]


def _message(client, validity, uid):
    from app.gtm.supymem.composio_gmail import DefiniteFailure
    status, data = client.uid("fetch", str(uid), "(BODY.PEEK[])")
    raw = next((item[1] for item in data or [] if isinstance(item, tuple) and isinstance(item[1], bytes)), None)
    if status != "OK" or raw is None:
        raise DefiniteFailure("Your reply is no longer in your inbox. Check your mailbox, then sync replies again.", status_code=404)
    message = BytesParser(policy=policy.default).parsebytes(raw)
    part = message.get_body(preferencelist=("plain", "html"))
    content = (part.get_payload(decode=True) or b"") if part else b""
    try:
        text = content.decode((part.get_content_charset() or "utf-8") if part else "utf-8", errors="replace")
    except LookupError:
        text = content.decode("utf-8", errors="replace")
    references = re.findall(r"<[^<>\s]+>", str(message.get("References", "")))
    root = references[0] if references else str(message.get("In-Reply-To") or message.get("Message-ID") or "")
    headers = {name: str(value) for name, value in message.items()}
    try:
        stamp = int(parsedate_to_datetime(str(message["Date"])).timestamp() * 1000)
    except (ValueError, TypeError, AttributeError):
        stamp = 0
    return {"id": f"imap:{validity}:{uid}", "threadId": root, "headers": headers,
        "raw": raw.decode("utf-8", errors="replace"), "internalDate": str(stamp),
        "payload": {"mimeType": part.get_content_type() if part else message.get_content_type(),
            "body": {"data": base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")}}}


def _quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def read_tool(slug, arguments):
    with _imap() as client:
        validity, last_uid = _checkpoint(client)
        checkpoint = f"{validity}:{last_uid}"
        if slug == "GMAIL_GET_PROFILE":
            return {"emailAddress": settings().username, "historyId": checkpoint}
        if slug == "GMAIL_LIST_HISTORY":
            prior_validity, prior_uid = map(int, arguments["start_history_id"].split(":"))
            if prior_validity != validity:
                raise ValueError("Your inbox was replaced. Reset your reply sync checkpoint, then sync again.")
            ids = [uid for uid in _search(client, "UID", f"{prior_uid + 1}:*") if prior_uid < uid <= last_uid]
            page = ids[:200]
            target = page[-1] if len(ids) > len(page) else last_uid
            return {"historyId": f"{validity}:{target}", "history": [
                {"messagesAdded": [{"message": {"id": f"imap:{validity}:{uid}"}} for uid in page]}]}
        if slug == "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID":
            ident = arguments["message_id"]
            if ident.startswith("imap:"):
                _, expected, uid = ident.split(":")
                if int(expected) != validity:
                    raise ValueError("Your inbox changed. Sync your replies again before opening this message.")
                return _message(client, validity, int(uid))
            if not re.fullmatch(r"<[^<>\s]+@[^<>\s]+>", ident):
                raise ValueError("Select a saved email message, then sync your replies again.")
            ids = _search(client, "HEADER", "Message-ID", _quote(ident))
            if not ids:
                from app.gtm.supymem.composio_gmail import DefiniteFailure
                raise DefiniteFailure("Your message is not in your inbox. Check your Sent mailbox, then sync replies again.", status_code=404)
            return _message(client, validity, ids[0])
        if slug == "GMAIL_FETCH_MESSAGE_BY_THREAD_ID":
            root = arguments["thread_id"]
            if not re.fullmatch(r"<[^<>\s]+@[^<>\s]+>", root):
                raise ValueError("Select a saved email thread, then sync your replies again.")
            ids = _search(client, "OR", "HEADER", "References", _quote(root), "HEADER", "In-Reply-To", _quote(root))
            return {"messages": [_message(client, validity, uid) for uid in ids[:200]]
                + [{"id": f"imap:{validity}:{uid}"} for uid in ids[200:]]}
        elif slug == "GMAIL_FETCH_EMAILS":
            criteria = []
            for token in arguments.get("query", "").split():
                if token == "-in:sent":
                    continue
                if token.startswith("from:"):
                    criteria.extend(("FROM", _quote(_address(token[5:]))))
                elif re.fullmatch(r"newer_than:\d+d", token):
                    since = datetime.now(timezone.utc) - timedelta(days=int(token[11:-1]))
                    criteria.extend(("SINCE", since.strftime("%d-%b-%Y")))
                elif re.fullmatch(r"(?:after|before):\d+", token):
                    direction, timestamp = token.split(":")
                    stamp = datetime.fromtimestamp(int(timestamp), timezone.utc)
                    criteria.extend(("SINCE" if direction == "after" else "BEFORE", (
                        stamp + (timedelta(days=1) if direction == "before" else timedelta())).strftime("%d-%b-%Y")))
                else:
                    raise ValueError("Use a sender or date filter for your local inbox, then try again.")
            ids = _search(client, *(criteria or ["ALL"]))
        else:
            raise ValueError("This mailbox operation is unavailable locally. Use your mailbox app instead.")
        offset = int(arguments.get("page_token") or 0)
        limit = min(200, max(1, int(arguments.get("max_results", 200))))
        selected = ids[offset:offset + limit]
        return {"messages": [_message(client, validity, uid) for uid in selected],
            **({"nextPageToken": str(offset + limit)} if offset + limit < len(ids) else {})}
