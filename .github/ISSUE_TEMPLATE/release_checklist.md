---
name: Desktop release checklist
about: Track your local desktop release gates, installers and rollback plan.
title: 'Desktop release: '
labels: ''
assignees: ''
---

Follow [Releasing](../../docs/self-hosting/RELEASING.md). Keep this issue open and
your release draft until every applicable gate has evidence. If a check fails,
record the blocker and your next step instead of marking it complete.
Never attach keys, environment dumps, private databases or contact details.

## Your candidate

- Version and exact `desktop-v<version>` tag:
- Reviewed public source commit and repository:
- Successful `desktop-local-release` run ID:
- Draft GitHub Release:
- Release owner:
- Blockers, evidence locations and next steps:

## Your source and safety gates

- [ ] Your changelog reflects supported behavior and remaining limitations.
- [ ] Your source export includes the changelog, this template, runbook and local tag workflow; private history and hosted publishing workflows stay out.
- [ ] Your exported tree passes `check_public`; record the command, exit status, file count and findings count.
- [ ] Your final public worktree and all reachable refs pass `check_public` with Gitleaks and complete history; record evidence separately from the export scan.
- [ ] Your exact source and installer payloads have cleared dependency, knowledge-data and artwork rights with required notices.
- [ ] Your local identity, displayed version and maintenance version metadata are verified against the candidate.
- [ ] Your changes preserve flag defaults, ownership, validation, accounting, consent and outreach checks.
- [ ] Your page reads use only configured self-hosted Firecrawl across every local path; the existing signals-reader fallback blocker is resolved and tested.

## Your installed app

- [ ] Your universal Mac DMG passes fresh install, restart and clean shutdown on Apple Silicon and Intel.
- [ ] Your x86-64 Linux DEB and AppImage pass fresh install, restart and clean shutdown.
- [ ] Your x86-64 Windows NSIS installer passes fresh install, restart and clean shutdown.
- [ ] Your packaged OS keychain saves, reads and deletes a disposable key on each platform; dependencies and Linux Secret Service/session access are present.
- [ ] Your sample journey works without a key in the actual package; sample results remain clearly fictional and no real mail or provider calls occur.
- [ ] Your live journey covers your idea, research, people, outreach, experiments and first booked call in two unrelated domains, one B2B and one consumer; record real outcomes and unresolved steps.
- [ ] Your saved model, search and mailbox settings configure the actual desktop transports; a healthy service alone is not sufficient.
- [ ] Your requested work does not ask for a second confirmation or expose usage amounts, and saving a connection starts no unrelated work.
- [ ] Your outreach retains source disclosure, opt-out, suppression, sender address, eligibility checks and caps.
- [ ] Your local data stays outside the app; network requests and the single-user security boundary match the documented privacy model.
- [ ] Your private backup and isolated restore are verified, including keychain recovery and schema-compatible rollback.

## Your publication and recovery

- [ ] All three matrix jobs pass; your downloaded artifacts belong to the recorded tag, source commit and successful push run.
- [ ] Your draft contains exactly the verified universal Mac DMG, Linux DEB, Linux AppImage and Windows NSIS installer.
- [ ] Your notes disclose unsigned/ad-hoc signing and manual updates; notarization, updater manifests and automatic updates are not claimed unless separately implemented and verified.
- [ ] Your draft installer bytes pass your platform checks after download, not only before upload.
- [ ] Your release is a prerelease, is not marked latest, and uses the existing verified tag.
- [ ] Your withdrawal owner, compatible previous installer or first-release fallback, and private pre-upgrade backup are recorded; rollback never moves tags or deletes data.
- [ ] Your release owner approves publication, then verifies the public installer assets and records the published release.
