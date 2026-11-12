# ONE-REVIEW-FAST-RELEASE-1112

Date: 2026-11-12. Driver-state: C-5345.

Owner, 2026-11-12:

1. One exact-head cross-family review per change. High/critical risk keeps an Opus or Sol reviewer as today. No second review, no `carbon-debt` label, no release gate on second-review debt.
2. Release train: one machinery release per day at 06:00 UTC, cut from main only when main changed since the last tag and is green; an urgent bug gets an immediate hotfix release; bugs are fixed fast in follow-up PRs.
3. The merge queue groups changes: wait about 10 minutes to collect entries so several PRs build and test together (fewer full CI runs).
4. UI changes: the local pages are the test copy; the Grok QA bot tests them after landing locally, before anything public.

The release train is the runbook in `docs/release.md`.

Decision 3 is GitHub's merge-queue parameter `min_entries_to_merge_wait_minutes`, which is 0. This repository does not write rulesets: the publisher's ruleset verbs are check, list and view, and there is no ruleset definition in the tree. The wait was not changed. It has to be set on `learn-ukrainian/machinery` and `learn-ukrainian/learn-ukrainian` by a ruleset edit this repo cannot make.
