# Epic-specific rules (drive-epic §0c, §0d)

Read before the first action on the named epic. Other epics skip this file.

## §0c. Hramatka epic — dual-repo queue (epic #4542 only)

If `SESSION_EPIC` is Hramatka (public #4542), the priority and ownership queue is the private
priority board, not the public epic body. Cold-start read order: **private priority board →
private open PRs → public PRs linked from #4542 only.** Public #4542 is a charter and a bare
pointer: never generate or mirror a public checklist from the private board. GitHub issue and PR
state in either repository stays the factual source for open/closed; if the private board and any
other queue view disagree, the private board wins and you correct the other view the same
session. Contract: `docs/runbooks/hramatka-driver-queue.md`.

Before a new dispatch, scope or PR, run
`.venv/bin/python -m lu_machinery.fleet.hramatka_scope_gate --action <action> --issue-repo <repo> --issue <n>`
(actions: `cleanup`, `escalate`, `new_dispatch`, `new_pr`, `new_scope`, `review`,
`unblock_own_open_pr`); only `ALLOW` permits the action. Before declaring a Hramatka handoff
verified-clean, run `.venv/bin/python -m lu_machinery.fleet.hramatka_hygiene_check`: only exit 0
is a pass, and exit 2 (`unknown`, GitHub unreachable) is never clean.

Host actions on the Hramatka host follow the §7-rollout table in
[review-merge-cleanup](review-merge-cleanup.md#7-rollout-local-and-production-proof-when-the-epic-requires-it).
A production release rollover on the live-serving host is a production cutover and needs a
present-tense operator GO for that rollover (an earlier or closed issue's GO does not count); the
private deployment runbook applies only after it. Host access and security configuration stay
operator-only.

## §0d. Core fresh lesson-based build (epic #7994, sub-epic #8397)

A1–B2 core modules are built fresh from new lesson plans by the fresh-build engine; `--upgrade`
is abandoned (`docs/epics/fresh-build-requirements.md` §1). Same `$drive-epic` loop, not a second
driver skill and not `$track-completion`. Read first
`docs/epics/fresh-build-build-program.md` (§1–§3, §5, §6), then the requirements, plan schema,
writer contract and review contracts as the phase needs. Seats live in `model-assignment.md`
(Ukrainian content authoring row); do not freeze a roster here.

**Done** is the learner URL on the level's track (`/<level>/<slug>/`), with Pages only on a
present-tense operator GO (§7-rollout) and the LU QA sweep triaged (build program §5 step 10).
Engine-on-`main` and gates green in a worktree are not done.

Binding for this lane:

1. **Nothing typed**: every Ukrainian string is a tool copy or the writer's resolved text; a
   defect is fixed at its layer (plan, pack, word store, prompt, gate or code), never by
   hand-editing a lesson or a generated file (R-12, R-35).
2. A module has as many lessons as its content needs (R-02); the arc's `est_lessons` is an
   estimate, never a target.
3. The plan fixes each lesson's activities (count, type, placement); the writer cannot add or
   change them. Activity volume, correctness and workbook variety are acceptance criteria.
4. The **driver does not decide Ukrainian**; the sources override any model (R-35). Language
   work and its reviews go only to sanctioned language lanes, cross-family to the author;
   Ukrainian reviews require the Sources MCP. If an AGY run ends `agy_background_task_canceled`
   (#8771), retry once, then reroute.
5. Review at scale is automated (review tooling WP 14–16, LU QA sweep); the operator reviews the
   pilot module and spot-checks after it.
6. Content PRs are **scripts-free**. While review or CI runs on unit N, prepare N+1; once gates
   pass in a worktree, open the PR the same session.
