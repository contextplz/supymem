# Standing approval policy

The policy `feature_flags.approvals_policy_v1` opts in when set to `true`. It defaults off.
Standing grants reuse `approvals` (`standing_policy`, `standing`), so there is no migration.
The existing request, list (`status=standing`), and withdraw endpoints serve the controls.
Each grant is bound to your person, venture, action type, and exact saved inputs.
Grant, revoke, and auto-decision audit rows commit in the same transaction as the decision.

Internal callers of `StandingAction` must supply verified current costs, task allowances,
recipient history, sequence approval, and daily usage. Missing limits fail closed.
Only low-risk `workflow_write` requests can currently consume a standing grant.
Their private `standing_policy_decision` records preserve the requested kind in the detail
and are not provider execution tokens. Grant, denial, and decision records stay founder-private.
All external execution and outreach compliance checks remain unchanged.

Private chat replies dispatch matching saved cards through the existing confirmation
admission and execution paths. Execution rechecks the exact live grant after permission
checks, including its founder, venture, scope, expiry and revocation, and commits an audit
decision before dispatch. Missing limits and all hard-rule actions still require a click.
Automatic receipts offer revocation and, for running jobs, the existing scoped stop control.
An Always choice saves a grant and approves the current action once. Settings lists and
revokes your active grants. Revocation takes effect at the next execution recheck.
