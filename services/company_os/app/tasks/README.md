# Serial goals

Set `goals_v1` to the boolean `true` in the existing `feature_flags` credits policy to enable goal contracts. Missing, false, and non-boolean values leave the existing planner behavior in place. Turning the flag off also stops new goal admissions and confirmations.

Goals use the existing Task, TaskRun, TaskStep, TaskEvent and jobs outbox. Steps run serially. Each step keeps its literal subgoal. The curated action inputs are validated against the models in `goals.py`. `output_bindings` maps a documented output field to a unique name; an input reference is `{"binding": "name"}`. References resolve only from an owned, completed earlier step after rechecking its source receipt.

The executable subset is customer search, invitation draft preparation, public-source experiment preparation/run, and reading the saved booking link. Search, draft and experiment actions retain their original exact confirmation cards. The card carries a task/run/step binding. Accepting it restores the original step and metering contexts; no goal or plan approval grants contact authority. No send, publish, payment, paid-ad, or LinkedIn automation action is exposed here.

The first template is `book N customer calls this week`. It reads your saved booking link, finds a customer contact, prepares an invitation for your review, and checks real booking replies from distinct contacted people during the original local week. Preparing an invitation does not book a call or send an email. You still review and send through the existing outreach workflow. The existing scheduler rechecks waiting booking receipts without executing another action.

Limits are 12 steps, the existing credit/token/tool/attempt ceilings, a hard deadline, and at most 24 hours elapsed work for a goal. A cap can stop a goal before the week ends. Plans are fixed in v1, so there are zero automatic replans, within the existing maximum of three strategies. Identical action/input attempts stop rather than replaying uncertain work. Stop retains saved results and prevents further admissions; an already in-flight provider call may finish.
