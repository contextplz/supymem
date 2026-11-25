# Exact nested modal source candidate

This is a local MIT-licensed patch against official `trycua/cua` commit
`7a8f66ad04e62fccb18cca9965f2964fcaee124e` (CUA Driver 0.29.1).
The manifest pins the complete Rust workspace, Cargo.lock, upstream license,
and patch bytes. It does not activate or replace the official binary.

## General behavior

The native AX observer discovers nested AXWindow/AXSheet surfaces only through
actual AX parent relationships, same AX PID, SPI CGWindowID, and fresh CG owners
for both child and parent. Discovery has a node/depth/time budget. Geometry,
labels, application-specific dialogs, and global input are not identity proof.
An exact observation root must be unique and retain fresh CG ownership. A
scoped modal walk excludes unrelated sibling sheets. Snapshots expose the
actual PID/window identity per node and parent window identity for native roots.

The adapter follows only a unique, independently re-enumerated same-PID modal
surface. It takes a new snapshot of that surface before presenting actionable
nodes. Unknown identity, protocol loss, cycles, closed surfaces, and ambiguity
refuse without input. Existing native mutation-time ancestry, snapshot/cache,
CG ownership, and background routing guards remain in force. Generic process
keyboard ambiguity and unverifiable effects continue to hold.

## Nearest native ancestry at mutation admission

Mutation admission resolves the nearest actual AXWindow/AXSheet ancestor before
considering `AXWindow`. Every retained ancestor must have the same native PID;
cycles, missing parents, depth/time-budget exhaustion and unknown roles hold.
A sheet requires positive SPI/CG ownership for itself and its actual native
parent. A containing-window hint can agree with that exact parent but cannot
select it instead of the sheet. Native CF identity, role, PID and SPI IDs must
agree; unrelated or contradictory hints hold. A missing hint cannot rescue
unavailable sheet identity. Native nested sheet parents are allowed only through
that exact positive relationship; geometry and array order are unused.

The existing mutation classification consumes the resolved nearest ID: another
ID remains `OutsideTargetWindow`, and unavailable identity remains `Unproven`.
Keyboard ambiguity/visibility and transport admission rules are unchanged.
Nine pure ancestry regressions pass; the native mutation-path regression is in
the clean-build filters but has not yet compiled/run. This establishes source
policy, not the actual AXWindow attribute behavior of any running dialog.

## Scoped press readback follow-up

The reusable native read-only probe captures exact AX ancestry, PID/CG identity
and a complete typed AXSheets inventory before one admitted mutation. Its callers are a single advertised plain background AXButton press and
window-scoped press_key/hotkey, under the existing per-PID mutation lease.
Key readback requires a positive window ID and admitted GenericKey lease,
background delivery, and no coordinate-focus branch. It captures after focus
setup and before the single key post; an implicit target uses only the exact
window native-ancestry check, with a bounded focused-element attribute read. Posting errors propagate without readback or
replay. Desktop/global/foreground/coordinate/lease-less paths gain no new
confirmation capability. Modifier-click transport is unchanged.
Successful AX dispatch alone still has `effect: unverifiable`.

The probe confirms only an exact child sheet opening under the retained root,
or an exact sheet closing with its retained same-PID parent still live. Missing,
malformed, duplicate, partial or over-budget inventories hold; sibling
replacement/multiple new sheets do not confirm. Closing requires a successful
empty **exact-window** CG query, never the ordinary enumeration's Unknown.
Native identities/actual parent relationships establish scope; geometry,
labels, screenshots, arbitrary tree changes and elapsed time do not establish
an effect. Readback makes no input calls and never retries an uncertain press.

Readback is bounded to three attempts with a 450 ms admission budget and 20 ms
AX message timeouts. The budget stops further work; it is not an absolute OS
scheduling guarantee. External activity can race any observation, so a scoped
transition receipt is neither universal causal proof nor final task success.
Independent goal verification and strict background/recipient acceptance remain
required. Other button effects, nested sheet parents, unsupported AXSheets,
and key effects without the supported exact modal postcondition continue to
hold. The existing vendor press_key value/selection oracle is preserved as a
separate behavior; new modal evidence never claims a text-value change. This
follow-up does not establish arbitrary button/key task completion.

The fourteen pure postcondition/dispatch/admission tests pass. Two new native
receipt tests are included in the clean-build filters. The new native probe/key/click wiring has
not yet compiled or run against a desktop. Root's earlier frozen candidate
build is separate and does not validate this follow-up.

## Reproduce source gates

Export `libs/cua-driver/rust` from the exact official commit into a pristine
workspace directory. Preserve the accompanying upstream license. The full
workspace digest rejects missing, added, changed, or symlinked source files.
Run from the Orchestrator checkout:

```sh
python scripts/desktop/build_cua_modal_candidate.py \
  --source-tree /absolute/path/to/pristine/rust \
  --output /absolute/path/to/new-candidate-directory
```

This verifies source/patch/lock/license pins, copies into a fresh private output,
applies the patch, and compiles/runs the actual vendor pure scope decision tests
and modal admission/postcondition tests with rustc. It creates `candidate.json` with acceptance
explicitly pending. It runs no daemon, application, input, or desktop observer.

For a clean native candidate build, add `--native-build`. This requires at least
4 GiB free and runs all eight filtered native source/schema test gates with
`--release`, followed by locked release builds of the driver and cursor helper.
They share the same owned target/profile; there is no separate debug dependency
build. Actual storage/time improvement has not yet been measured. There is no installation or
automatic sidecar staging. Native build and actual desktop acceptance have NOT
passed yet; the first native compilation was stopped for disk headroom, and
its exclusively owned target was removed.

## Deliberately disabled packaging seam

The existing `prepare_cua_driver.py` default remains the pinned official archive.
`--reviewed-source-build /candidate-directory` refuses while `reviewed-build.json`
is absent. That file must be committed only after root reviews native trials;
it binds candidate-manifest bytes, both artifact digests, and the exact digest
of `native-trial-receipt.json`. After company signing, regenerate the candidate
artifact digests and freeze the manifest bytes in the review ledger; the initial
source build records unsigned candidates. Both artifacts must have valid strict company
signatures (Team Q9YFTMS7DP) before either packaged destination changes. The
build script never creates the review ledger or marks acceptance passed.

A future reviewed ledger uses schema `supymem_reviewed_cua_build_v1` and fields
`candidate_manifest_sha256`, `binaries_sha256` (keys `cua-driver` and
`cua-cursor-theme`), and `reviewed_native_trial_receipt_sha256`. The review ledger
is authority established by the source reviewer, not a model-generated success
claim or a self-attestation from the candidate build script.

## Evidence and remaining acceptance

The unpatched pure vendor decision module failed three new regressions: exact
sheet selection, closed CG ownership despite matching stale AX, and duplicate
root admission. Patched source scope tests pass 15 cases; pure modal admission
passes six cases. Python adapter/build/vocabulary gates pass 124 tests.

Still required: full native compile and schema tests; root-controlled read-only
nested-sheet observation; modal click/save with independently verified output;
zero decoy input and strict sampled foreground gates through teardown; stale
sheet/foreign sibling/Stop/resume behavior; cleanup gates; branded overlay and
signed packaged-app acceptance. These source results do not establish those
runtime results or broad desktop task success. Linux/Windows are unchanged;
this candidate extends the macOS native AX surface observer only.
