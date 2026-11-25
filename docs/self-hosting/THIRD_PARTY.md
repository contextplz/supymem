# Third-party components and redistribution

Supymem code is licensed under the [Apache License 2.0](../../LICENSE), with
project attribution in [NOTICE](../../NOTICE). That grant does not replace
upstream software licenses or grant rights to third-party data, artwork or marks.

This inventory records local license and provenance evidence. No upstream fetches
or release builds were run. Inspect the installer lockfiles and actual release payload separately.
Preserve every applicable upstream license, copyright and notice, including
modification notices, when you redistribute.

## Bundled files and ports

| Component and repository paths | License and local evidence | Redistribution status |
| --- | --- | --- |
| GStack: `third_party/gstack/openclaw/skills/{gstack-openclaw-office-hours,gstack-openclaw-ceo-review}/SKILL.md`; Python adaptations under `services/company_os/app/gstack/` | MIT; `third_party/gstack/LICENSE`, `NOTICE`, `PINNED_SHA`; pin `d078622b73539fc1a7a27e709861e9b6b058ae98` | Retain the complete MIT license and notice. |
| OpenWorker: `guard.py` and `providers.py` in `packages/core/research_web/` | MIT, Andrew Ng; accompanying `LICENSE` and `PROVENANCE.md` pin `9702c86c7f90425d93fb2de877833ead2f446686`. Supymem modified `guard.py`. | Retain the complete MIT license and provenance record. |
| Huginn Python port, `packages/huginn_watch/` | MIT, Andrew Cantino, Huginn contributors and Company OS; accompanying `LICENSE`, `README.md`, `pyproject.toml` | Compatible; the package retains its MIT metadata and license rather than being relabeled Apache-2.0. |
| QM harness, plugins, CLI and taste-skill | MIT, QM contributors and Leonxlnx; upstream notices kept in the private checkout | **Excluded from the public source.** These are not part of Supymem Local. Preserve upstream terms and notices if redistributed separately. |
| GBrain algorithms, `services/company_os/app/gbrain/{__init__,briefing,cycle,graph,links,sanitize,think,verbs}.py` | `__init__.py` attributes the port to Garry Tan, version `0.46.21.0`, MIT; the upstream revision and full copyright notice are not available offline. | **Excluded from the public source.** Before redistribution, confirm the revision and adapted source paths and retain their complete MIT notice, or replace the port. |
| Paperclip algorithms, `services/company_os/app/paperclip/` | MIT, Copyright (c) 2025 Paperclip AI, according to the package's `__init__.py` | Root `NOTICE` contains the copyright notice and complete MIT terms. Retain them; the absent `vendor/paperclip/LICENSE` reference clone is not a missing source-tree notice. |
| Munder Difflin algorithms, `services/company_os/app/munder/` | MIT, Copyright (c) 2026 Chaitanya Giri, according to the package's `__init__.py` | Root `NOTICE` contains the copyright notice and complete MIT terms. Retain them; the absent `vendor/munder-difflin/LICENSE` reference clone is not a missing source-tree notice. Non-commercial LimeZu artwork is not covered by these code terms. |
| DeepSeek Harness Python adaptation, `packages/harness/{__init__,inbox,invariant,loop,prompt,session,tools,types}.py` and `pyproject.toml` | `vendor/README.md` calls the reference MIT; the port's manifest declares Apache-2.0. Neither establishes the revision, upstream file mapping or a complete license notice. | **Excluded from the public source.** Before redistribution, document independent implementation or identify the source revision and retain the full applicable terms, or replace the port. |
| GTM cofounder skill pack, `.agents/skills/gtm-cofounder/` | MIT, AIDevGTM/gtm-cofounder version `1.3.0`, according to its `SKILL.md` | **Already outside the public export.** Confirm the revision and obtain the complete upstream MIT notice before you redistribute the pack. |
| CUA: `scripts/desktop/vendor/cua-modal-0.29.1/exact-modal.patch` and its manifest; driver and cursor-theme sidecars | MIT, Cua AI, Inc.; the patch and `LICENSE.upstream.md` match the manifest digests for the patch against pin `7a8f66ad04e62fccb18cca9965f2964fcaee124e`. Separate `src-tauri/third_party/CUA_LICENSE.txt` covers CUA software. | Retain both notices. The tracked binary placeholders are not evidence about a staged executable or its cursor assets. Inspect them before redistribution; they are not local installer sidecars. |
| Peekaboo sidecar, configured in `src-tauri/tauri.conf.json` and staged by `scripts/desktop/prepare_peekaboo.py` | MIT, Peter Steinberger; `src-tauri/third_party/PEEKABOO_LICENSE.txt` | Compatible; no executable is tracked here. Verify the staged release binary and retain the license. |
| Patched `mac-usernotifications` 0.3.1, `src-tauri/patches/mac-usernotifications/` (`src/*.rs`, crate manifests, lockfile and README) | `MIT OR Apache-2.0` per the crate metadata. `LICENSE-APACHE` provides the full selected Apache-2.0 text; `PATCH.md` identifies the crate source and local changes. | Retain both files. When the original crate archive is available, check its exact Git revision and any additional upstream notices. |
| Frozen `companyos-runtime` sidecar and `libswiftCompatibilitySpan.dylib`, referenced in `src-tauri/tauri.conf.json` | Generated or externally supplied; this checkout does not contain a complete license inventory for embedded Python libraries or the Swift compatibility library | Unverified release contents. Inventory the actual packaged files and their licenses before distribution; the project license alone is insufficient. |
| PMF workbook and extraction, `data/knowledge/startup-pmf-2026-09-19/`; runtime `services/company_os/app/knowledge/startup_pmf.json.gz` | No redistribution license recorded; dataset `README.md` and manifest identify a user-supplied workbook and external sources | **Excluded from the public source.** Obtain redistribution permission for the workbook, extracts and derived runtime before bundling them. |
| YC video advice, `data/knowledge/yc-video-advice-1dfdaf90/`; runtime `services/company_os/app/knowledge/yc_video_advice.json.gz` | `UPSTREAM_PIN.json`, manifest and README: upstream MIT covers code only, not transcript content; pin `1dfdaf90b3a5d4074a7f79ca21fd4e9337633d5a` | **Excluded from the public source.** Approval of short attributed excerpts is not a redistribution license. Clear excerpt/runtime rights before bundling; full transcript redistribution and fine-tuning are not approved. No Y Combinator affiliation or endorsement. |
| Charlie mascot, `apps/console/public/charlie/` and artwork in `src-tauri/splashscreen/swing/` | No asset license or ownership record found; GLB metadata says `procedural mascot builder`, which is not proof of rights | **Excluded from the public source.** Confirm ownership and license of the model, textures, animations and images before redistributing them. |
| Brand and connection artwork: private `src-tauri/icons/`, splash logos, `apps/console/src/app/icon.svg`, console public logos and provider marks, and marketing assets including `apps/marketing/public/cursor-logo.png` | No per-asset license inventory found; provider marks remain subject to their owners' rights | **Excluded from the public source.** The export generates replacement icons and marks under the project license; verify rights before redistributing any private artwork. |
| Shopping canary data, `data/seed/`, `data/images/` | `data/seed/manifest.json` records `source_mode: synthetic`; these data and provenance notes are omitted from the public export | Current seed is synthetic, not proof that Amazon-derived data can be redistributed. Amazon Reviews 2023 has no simple open redistribution grant recorded; review rights for any regenerated non-synthetic release data. |

`packages/desktop_runtime/agent_desktop.py` implements an optional desktop tool,
not an additional verified bundled executable. Referencing or signing a binary
does not verify the license of its contents.

## Optional vendor checkouts

Only `vendor/README.md` and `vendor/skills/MANIFEST.yaml` are tracked under
`vendor/` in this checkout. The clones described by those files and
`scripts/sync_vendors.sh` are absent. The following licenses are recorded in
repository documentation, not independently verified upstream license texts.
Before bundling a clone, verify its exact revision and retain its license and
notices. A bridge or out-of-process service is not an automatic copyleft exemption.

| Referenced directory under `vendor/` | Recorded license | Apache-2.0 redistribution assessment |
| --- | --- | --- |
| `agent-reach/` | MIT; `docs/supymem/28_OPEN_SOURCE_AND_SERVICES_REUSE.md` | Compatible if verified and notices retained. |
| `gpt-researcher/` | Apache-2.0; same reuse document | Compatible if verified and required notices retained. |
| `open_deep_research/` | Not specified beyond "various" in the reuse document | Unknown; obtain the exact upstream license before bundling. |
| `openworker/` | MIT; reuse document and bundled web-module license | Compatible if the clone's license and contents are verified. |
| `hermes-agent/`, `openclaw/`, `picoclaw/` | MIT according to `vendor/README.md` | Compatible if verified and notices retained. |
| `huginn/` | MIT; `vendor/README.md` and bundled port license | Compatible if verified and notices retained. |
| `opencode/` | MIT; reuse document | Compatible if verified; its separately installed CLI also needs a release inventory. |
| `gbrain/` | MIT; `vendor/README.md` and port attribution | Compatible if verified and notices retained. |
| `openhands-sdk/` | MIT; `vendor/README.md` and reuse document | Compatible if verified; the agent-server container has additional dependencies. |
| `postiz/`, `recordly/` | AGPL-3.0 according to `vendor/README.md` | **Incompatible with Apache-only redistribution.** Keep their AGPL terms and satisfy source/network-use obligations if distributed or integrated; reference-only status does not relicense them. |
| `paperclip/`, `deepseek-harness/` | MIT according to `vendor/README.md` | Compatible if verified and notices retained. |
| `munder-difflin/` | MIT source code; LimeZu artwork under `src/renderer/src/assets/` is non-commercial according to `vendor/README.md` | Code is compatible if verified. **The artwork is incompatible with unrestricted Apache-2.0/commercial redistribution.** Do not bundle it under the project license. |
| `research/openleads/` | PolyForm Noncommercial according to `vendor/README.md` | **Incompatible with unrestricted Apache-2.0/commercial redistribution.** Study only; do not import, port or bundle under the project license. |
| `research/email-supymem/` | No license recorded in `vendor/README.md` | Unknown; obtain a license grant and audit any copied backend material before release. |
| `research/opengtm/` | MIT according to `vendor/README.md` | Compatible if verified and notices retained. |
| `research/scrapy/` | BSD-3-Clause according to `vendor/README.md` | Compatible if verified and notices retained. |
| `research/crawl4ai/` | Apache-2.0 according to `vendor/README.md` | Compatible if verified and required notices retained. |

## Optional skill packs

`vendor/skills/MANIFEST.yaml` pins seven packs. Their checkout directories are
absent here; the manifest is evidence of declared terms, not a substitute for
the upstream licenses. Existing license and export restrictions remain unchanged.

| Manifest pack | Declared license and attribution | Redistribution assessment |
| --- | --- | --- |
| `gtm-skills` | MIT, LeadMagic LLC | Compatible if you retain its license and attribution. |
| `growth-skills` | MIT, Peter Foy | Compatible if you retain its license and attribution. |
| `marketing-skills` | MIT, Corey Haines | Compatible if you retain its license and attribution. |
| `anthropic-skills` | Mixed: Apache-2.0 generally; `docx`, `pdf`, `pptx`, `xlsx` have Anthropic source-available terms, all rights reserved, `redistribute: false` | **Restricted skills are incompatible with Apache-2.0 redistribution.** Do not bundle or export those four skills; verify every other skill's actual license. |
| `superpowers` | MIT, Jesse Vincent | Compatible if you retain its license and attribution. |
| `voltagent-index` | MIT, VoltAgent | Compatible for the index if notices are retained; listed third-party skills need their own review. |
| `gstack` | MIT, Garry Tan | Compatible if you retain its license and attribution; the two actually bundled skills are covered above. |

## Release checks

- Review and scan the actual public source export before publishing it. It does
  not include GBrain, the optional Python harness, the PMF and YC corpora, or
  private artwork. Keep these materials out unless you verify redistribution rights.
- Before publishing binaries, inspect the exact installer payload from each
  platform and verify all bundled software, data and artwork rights. A source
  export does not clear installers built from a private checkout.
- Keep the root `LICENSE` and `NOTICE`, nested upstream licenses and notices,
  and any required source offers in your actual source and binary distributions.
- Verify optional clones, container images and each bundled runtime dependency
  from the exact release artifacts; do not infer licenses from the project grant.

The root `LICENSE` is a byte-for-byte copy of the trusted local Debian file
`/usr/share/common-licenses/Apache-2.0` (SHA-256
`cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`).
The full official text includes sections 1 through 9 and the unmodified appendix
with its template copyright line. Project attribution belongs in `NOTICE`, not
in that template.
