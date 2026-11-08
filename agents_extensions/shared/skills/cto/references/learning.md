# Learning from mistakes

A closed loop for the CTO's own mistakes: a wrong claim, a wasted seat, a missed check, an
operator correction. Practices used: blameless postmortems (Google SRE book, ch. 15) and
the "5 whys" (Toyota Production System). Recovery over gates
([review policy § 5](../../review-loop/references/policy.md#5-recovery-over-gates)) binds.

Where things go: CTO process mistakes go in this private ledger; system and production
defects go in `docs/bug-autopsies/` (public, one per root cause); consequential decisions
go in the decision journal (`docs/decisions/`).

## 1. Record

Each mistake gets one private tracker item: `lu-driver-state add --lane cto --kind commitment`
with an alias prefixed `ML-`, an owner and a done condition:

- statement: date (`date -u`), what happened as fact, impact and root cause in the CTO's
  process. Ask why until you reach why the process allowed it ("I forgot" is not a cause);
- done-when: the corrective change landed;
- evidence: the PR or commit, recorded with `lu-driver-state evidence <id>`.

Operator corrections are always entries, kept private in the tracker.

## 2. Fix at the source

Revert or fix forward, then fix the cause at the source: code, a test, a lint rule or one
line in a skill. Do not add a refusal check, wrapper, checklist or ritual for a single slip.
The entry closes on that landed fix: record the PR or commit with `evidence` before setting
`status <id> done --reason <reason>`. A lesson that changes nothing is not a lesson.

## 3. On a repeat

A repeat is a `lu-driver-state note <id> <text>` on the same item, with the recurrence count.
Reopen it with `status` and fix the cause again at the source; there is no escalation ladder.

## 4. Read it

At session start use `lu-driver-state list --lane cto --kind commitment --all` to locate
the `ML-` items, then `show <id>` for every open entry and the last ten done ones. The
pre-post self-audit ([decisions](decisions.md#before-posting)) lists the open entries as
known traps.

## 5. Measure it

The handoff scorecard counts new mistakes and repeats this session. A repeat is reported
to the operator with its source fix.
