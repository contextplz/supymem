# Public release checklist

Source review date: November 15, 2026. This document does not approve publication.

## Before publishing

- Keep your private checkout and history intact. Prepare a separate history-free export and review every candidate public ref.
- Exclude real `.env`, keys, credential-tool folders, private data, local databases, encrypted settings, logs, spending receipts, and backup archives. Encryption does not make them public.
- Run the existing exporter and public guard. A missing scanner, shallow history, unreadable file, malformed report, or remaining finding must block publication. Do not suppress findings by weakening the guard.
- Investigate suspected credentials privately. Revoke any confirmed exposed key and remove it from every public ref; never record its value in an issue or command.
- Review binary attachments, archives, asset metadata, LFS objects, and release artifacts separately. Text scanning is not proof of their safety or redistribution rights.
- Retain root LICENSE, NOTICE, and required upstream notices. Review [the inventory](THIRD_PARTY.md) for uncleared data and artwork. Do not treat an export allowlist as a license grant.
- Run installed acceptance on every target and preserve actual failure evidence. Repeat customer search and requested scans live; offline completion does not pass this gate. Earlier failed pages are not publishable; check new builds through your connected coding tool before claiming completion. See [Known issues](../../README.md#known-issues).

## Must-resolve upstream notices

Do not publish copied algorithms without retaining their complete upstream copyright and permission notices. Root [NOTICE](../../NOTICE) now contains the Paperclip and Munder copyright notices and complete MIT terms. Attribution alone is not a complete license notice.

- `services/company_os/app/paperclip/`: retain root NOTICE. The referenced `vendor/paperclip/LICENSE` clone path is absent; that is not an absence of the source-tree notice.
- `services/company_os/app/munder/`: retain root NOTICE. The referenced `vendor/munder-difflin/LICENSE` clone path is absent. Do not include separately licensed non-commercial artwork.

Review the notices against the exact upstream revisions and include them in your actual source and binary distribution. Other unresolved data or asset rights remain separate release gates.

## Build a separate public tree

Use the [maintainer export and scan recipe](../RELEASING.md#prepare-the-public-source), including its Python 3.12 environment with the app and test dependencies. The exporter uses an explicit allowlist and excludes private history and records; review its actual output. A nonzero guard result blocks release. Preserve required runtime files instead of deleting them to pass a scan. A snapshot scan does not scan the private repository's full history. Scan every ref in the separately reviewed candidate public repository before publication.

## Preserve the local product and safety

Your local desktop uses saved connections, built-in reading and keyless search, and owner capability switches. No hosted identity service, platform wallet, hidden flag, policy row, administrator, or old hosted bridge compatibility is required to enable a shipped local feature.

Do not weaken ownership, URL validation, atomic storage, backup validation, consent, suppression, disclosure, opt-out, postal address, or send caps. Guessed emails stay off. Saving credentials or enabling a switch must not start work you did not request.

Follow [Releasing](RELEASING.md#release-gates) and [acceptance](ACCEPTANCE.md). Keep the release draft until source scans, notices, installed startup, private storage, settings, and recovery gates pass with evidence from the actual candidate.
