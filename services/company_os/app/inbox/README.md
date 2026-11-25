# Read-only inbox briefings

`feature_flags.inbox_briefing_v1` requires the exact value `true`. Missing or false disables reading, classification, messages and tick enqueueing. The mailbox selection contract retains its legacy behavior when `multi_gmail_v1` is off.

The worker runs `inbox.briefing.tick` every five minutes for window delivery and pending-receipt recovery. Each inbox reads at most once every `poll_minutes` (default 60). Reads use only authorized, consented mailbox references and existing Composio read helpers. Mail is data, never instructions or permission to take an action.

Newly connected inboxes get one private consent offer. The offer itself does not read any mail. Running experiments boost known contacts using existing sent-touch metadata; inbox classification never invents an experiment reply receipt. Real reply counts stay with the existing account-aware reply worker and experiment readouts.

Chat actions use the existing authenticated chat endpoint. Only you, as the workspace owner, can change your inbox consent, schedule or rules:

- `inbox.briefing.consent`: `{"mailbox_id": "local-id", "consent": true}`. False stops reads and deletes that inbox's items and any generated messages containing them, including mixed-inbox briefings.
- `inbox.briefing.settings`: `{"timezone": "UTC", "windows": ["08:30", "17:30"], "quiet_hours": {"start": "21:00", "end": "08:00"}, "poll_minutes": 60, "email_copy": false}`. An empty payload reads the current settings, including `mailboxes: [{mailbox_id, consent}]`.
- `inbox.item.feedback`: `{"item_id": "local-id", "category": "important"}`, or `"ignore"`. Optional `scope` is `"sender"` (default) or `"domain"`.

Replies include `inbox_briefing` with the saved result. Briefings are posted to your private, idempotently created **Your inbox** chat conversation, never a shared or another founder's conversation. Message kinds are `inbox_briefing` and `inbox_urgent`. Citations include `inbox_briefing: {items, filtered, recommended_action}`. Item cards contain a suggested Gmail link and feedback actions. They do not contain full bodies or attachments. No action is executed automatically.

Minimal item metadata, ignored-message dedupe IDs and generated mail-bearing messages expire after 14 days. Consent and your feedback rules remain. The daily model cap is 300 messages across your inboxes; overflow and unavailable classifications stay explicitly unclassified. Urgent delivery slots are reserved before posting and capped at three per UTC day. Both quotas use UTC days so editing your schedule's timezone cannot reset them. Your briefing windows and quiet hours use your chosen timezone. Quiet hours suppress all delivery.

Local startup calls `store.ensure_schema`; Alembic revision `s057inboxbrief` creates the same mutable sidecars without rewriting append-only tables. The existing daily retention handler also purges these sidecars when briefings are disabled.

Email copies are deliberately rejected: this lane is read-only and never sends email. Large polling pages are backfilled before advancing the checkpoint. A page with 200 or more messages in the same second needs provider pagination before that cursor can advance; no unseen messages are intentionally skipped.
