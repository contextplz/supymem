# Releasing your local desktop app

Follow the [maintainer release procedure](../RELEASING.md#prepare-the-public-source) to export, scan, test, and build from public source. Never publish the private checkout or its history.

## Release gates

Record the reviewed public commit, OS, artifact hashes, and observed results in [the checklist](PUBLIC_RELEASE_CHECKLIST.md). Keep the release draft until the checks for each published artifact pass.

- Review [third-party notices and rights](THIRD_PARTY.md), the actual payload dependency inventory, and the export. A pattern scan is not a license grant.
- Use a clean installation and a copy of existing data, not your original workspace. Check startup, restart, shutdown, private owner sessions, key storage, settings import, and recovery. Use [desktop startup smoke](DESKTOP.md#headless-packaged-startup-check) and [acceptance](ACCEPTANCE.md) separately.
- Keep sample results separate from real model, research, mail, publication, and visitor evidence. A Ready badge is not execution proof. Verify builds through your connected coding tool before you claim they work.
- Verify updates, staged import, and full restore in disposable storage. Preserve encryption recovery material separately. Failed backup or validation must stop replacement.
- Keep ownership, URL validation, consent, disclosure, opt-out, suppression, postal address, and caps. Guessed emails stay off. Saved connections must not start unrelated work.

## Roll back

Stop all writers. Preserve the failed folder, restore a matching full backup and separately preserved encryption material, then use its matching app version. Never delete a populated table, merge databases, or create a new key to decrypt old settings. Follow [backup recovery](backups-security.md#restore-and-verify) and retain every copy until verification succeeds.
