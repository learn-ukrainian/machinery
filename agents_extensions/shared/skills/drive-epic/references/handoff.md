# Handoff and fleet-comms state (drive-epic §8)

Read before ending a session or writing a handoff. The §8a inbox drain comes first and is stated
once in the core `SKILL.md` loop; record any action or unresolved request from it in the lane
tracker, and never call the handoff complete because a one-shot worker acknowledged a message.

## §8. Handoff

End on your seat's handoff signal (the canary FAIL-HANDOFF for gemini; the SessionStart and
`thread-rollover` chain for Claude and Sonnet), not on a compact count. The driver-state database
is the lane's handoff.

### Record work throughout the cycle

Use `lu-driver-state add --lane <lane>` for every new order, commitment, goal, in-flight task,
blocker, decision and next action, with the matching kind, an owner and a done condition. Record
goals under their program's `operator_order` with `--parent <id>`. Every `inflight` item carries
the task ID and the FULL run nonce (`.venv/bin/python -m lu_machinery.dispatch wait <task-id> --run-nonce <nonce>`
compares it exactly).

Update items with `link` (references), `note` (progress) and `evidence` (proof). Never delete an
item or replace unfinished items with a session summary. Use
`lu-driver-state status <id> done --reason <reason>` only after evidence supports the done
condition; `dropped` needs an operator reference in its reason. Cite item IDs in cycle reports.

### Reconcile and hand off

1. Timestamp with `date -u +%Y-%m-%dT%H:%MZ`.
2. Reconcile `lu-driver-state render --lane <lane>` and `lu-driver-state check --lane <lane>`:
   every open order, goal, commitment, in-flight task, blocker, decision, parked item and next
   action has an item.
3. Prepare the rollover with the `thread-rollover` skill. Add one `handoff_note` item
   (`lu-driver-state add --lane <lane> --kind handoff_note`) with an owner and a done condition:
   timestamp, session identity, lineage and rollover IDs, open PR heads and gates, live task IDs
   with FULL run nonces, armed waits, residual owners and resume conditions, next actions. Cite
   work item IDs; update those items rather than duplicating their history.
4. Keep `.claude/<lane>-epic/DRIVER-STATE.md` under 4 KB (`wc -c`): pinned provider facts plus the
   render/check instructions only; no handoff block, no tracker state.
5. Write the durable brief rules core P7 requires, then complete the Fleet receipt below. Public
   briefs follow rules core P8; private detail stays in the driver-state database or the private
   repository.

On a Hramatka drive, run the Hramatka hygiene check before declaring the handoff verified-clean
([epic-specific](epic-specific.md#0c-hramatka-epic--dual-repo-queue-epic-4542-only)). Attach the
driver breadth report output ([routing-and-dispatch](routing-and-dispatch.md#3-routing-mandatory-routing_card_v1--breadth-operator-go-2026-08-06)).

**Skill source of truth is git.** Edit only `agents_extensions/shared/skills/drive-epic/`; deploy
copies such as `.claude/skills/` are overwritten on the next deploy.

| Sink | What to write | Survives |
| --- | --- | --- |
| **Driver-state database** (`lu-driver-state`) | Work items, evidence, history, one `handoff_note` | Durable lane handoff |
| **Pointer and durable brief** (`.claude/<lane>-epic/DRIVER-STATE.md`; rules core P7 brief) | Pinned provider facts and render/check instructions; supplemental continuity | Local pointer / brief |
| **Fleet-comms channel** | Issue and PR numbers only | Plane authority |
| **GitHub issues** | Residual and next work (the CTO files them) | Public queue |

```bash
# Fleet receipt: issue/PR numbers only (no residual tables, no secrets)
.venv/bin/python -I -m lu_machinery.fleet_comms channel publish <stream-channel> \
  "handoff: tracker=SSOT. Next issues #… Merged PRs #…" \
  --sender "$SESSION_HANDOFF_AGENT" --source <harness> --kind state \
  --idempotency-key "handoff-<epic>-<date>"
```

## Fleet-comms state

Fleet Comms is the durable authority for messages and jobs (plane mode `authority`; query the live
mode at orient); legacy broker and channel stores are read-only inputs. Live-driver diagnostics:
`.venv/bin/python -m lu_machinery.session_streams handoff-status --stream <stream-id>`
(for example `epic:4542`). A live driver never runs `handoff-claim`: the launcher already
claimed the lease, and remote recovery uses Monitor TTL/CAS or an attributed operator release,
never local PID observations or `--local`. Never flip the plane, enable retention apply or invent
a competing comms design from this skill; those stay the infra lane's gated actions.
