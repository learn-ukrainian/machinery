## Resume and confirm

In the fresh task, use the exact IDs and handoff path returned by `detect`.
Read that handoff before continuing. Finish the exact native title reconciliation
or the recorded unsupported-adapter fallback described in [preparation](prepare.md).
Use the replacement already bound to this rollover.

For a pending packet, bind and resume the exact replacement:

```bash
.venv/bin/python lu_machinery/orchestration/thread_handoff.py bootstrap-replacement --agent <agent> --lineage-id <lineage-id> --rollover-id <rollover-id> --replacement-thread-id <exact-replacement-task-id> --evidence "exact replacement binding"
```

For an already resumed packet, proceed directly to confirmation:

```bash
.venv/bin/python lu_machinery/orchestration/thread_handoff.py confirm-replacement --agent <agent> --lineage-id <lineage-id> --rollover-id <rollover-id> --replacement-thread-id <exact-replacement-task-id>
```

The lower-level `resume` and `confirm-started` commands remain available for
native adapters that have already registered the replacement:

```bash
.venv/bin/python lu_machinery/orchestration/thread_handoff.py resume --agent <agent> --lineage-id <lineage-id> --rollover-id <rollover-id> --replacement-thread-id <exact-replacement-task-id>
.venv/bin/python lu_machinery/orchestration/thread_handoff.py confirm-started --agent <agent> --lineage-id <lineage-id> --rollover-id <rollover-id> --new-thread-id <exact-replacement-task-id>
```

Confirmation checks the persisted replacement binding and lease. It requires no
semantic snapshot, quiz, verdict, or canary proof. Exact retries require the same
clean, continuous checkout and are idempotent; a different replacement ID fails
closed. A retry also repairs the registry projection after a partial write. The
lineage, rollover ID, generation fence and native identity/title rules remain in
force.
Pass `--stream <stream-id>` to confirmation when a bundle upload is required;
the configured `--monitor-base-url` carries through to the upload.

Successful confirmation unlocks the existing cleanup authorization. Before
archiving the predecessor, follow [archive proof](archive.md): authoritative app
state must show the exact predecessor idle and unpinned. Missing or ambiguous
app evidence preserves it.
