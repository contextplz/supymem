# Public release findings

## Current review, November 15, 2026

The current source has a local Tauri configuration, standalone-runtime preparation, encrypted saved connections with `.env` import, owner credentials and exact-origin checks, built-in reading and keyless search, owner capability switches, and local publication transports. These are source facts, not proof of a published or fully accepted installer.

The November 15 live checks did not complete customer search or requested scans. Earlier failed pages are not publishable; use [release acceptance](ACCEPTANCE.md) to check new builds through your connected coding tool. The ad-hoc Mac build also showed keychain prompts and loading trouble. Read [Known issues](../../README.md#known-issues) for newer evidence and [troubleshooting](troubleshooting.md) for next steps.

## Readiness review on November 7, 2026

Earlier export scans and dependency/launch checks were historical results against earlier trees. They do not certify today's source, private history, payload, or live workflows. Old missing-settings and unauthenticated-loopback descriptions are not the current contract. Re-run the existing exporter and guard on your reviewed candidate, then complete [release gates](RELEASING.md#release-gates).

## Decisions before release

Keep the private source history, real keys, customer data, and uncleared assets out of public distribution. Preserve required runtime files and license notices. Paperclip and Munder copyright notices and complete MIT terms are present in root NOTICE; absent reference-clone paths alone are not missing source-tree notices. [The inventory](THIRD_PARTY.md) still separates notices from unestablished data and artwork rights.

No fresh install, GUI, live mail, publishing, or native-platform acceptance is performed by this documentation review. Do not substitute old scan or unit-test counts for those checks.
