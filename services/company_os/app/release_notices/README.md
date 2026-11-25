# Release notice integration

This package verifies the existing public updater contract. `latest.json` policy
is authenticated by fixed-origin HTTPS, **not a cryptographic JSON signature**.
The artifact is SHA-pinned at its immutable version URL and its modern Minisign
signature is checked with the production Tauri public key. Both digests are
computed from the same streamed bytes before an unscoped, append-only public
witness is committed. That trusted witness permits later current-signature
checks without fetching the artifact again. It contains no person, tenant,
device name, address, release prose or token.

Only explicit `mandatory: true` or a current device below the existing
`min_version` floor qualifies. Ordinary updates remain off. No security meaning
is inferred from prose; a security-only event requires an actual future publisher
contract. Unknown device versions cannot establish eligibility.

The canonical engine registers `app_release` as `product_updates`, calls
`reconcile()` from the existing hourly `email.schedule` handler, and revalidates
this source directly in `deliver()` before rendering or provider dispatch.
There is no public ingestion/enqueue endpoint or recipient override. Both
lifecycle enablement and the explicit `app_release` template allowlist entry are
required before any source HTTP call.

`ReleaseUnavailable` defers the existing outbox one hour with a content-free
source-unavailable reason. It does not pass through the general trigger helper's
catch-all/None wrapper. A returned `None` means confirmed optional/superseded
policy or changed current ownership/version, and follows canonical source
ineligibility suppression.

Fresh policy and exact current nonrevoked owned Mac/version are checked at
delivery. Frozen provider payload, recipient custody, preferences, suppression,
quiet period, caps, idempotency and retry settlement stay canonical. HTTP permits
no redirects or environment proxies, requires normal TLS, refuses compression,
and bounds manifest bytes, artifact bytes, total time and each read. Transport
cancellation propagates without persisting a partial witness or queuing mail.

Tests use synthetic bytes with genuine fixture signatures, offline HTTP
transports and owned SQLite/migrated PostgreSQL databases. The PG tests use
actual template registration and delivery revalidation,
including unavailable-source deferral and recovery through the registered
worker with one saved message and one log-provider delivery. This is not a
published/notarized app, live delivery, installed updater or public-feed
acceptance. Version `0.1.84` in fixtures does
not assert that its signed draft has been published. Root owns CI admission and
release/installed/live-mail acceptance.
