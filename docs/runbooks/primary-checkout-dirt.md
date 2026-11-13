# Primary-checkout dirt: audit and self-cleanup (#11253)

Dispatch v2 does not refuse a dirty primary checkout. Agents must still keep
the primary checkout read-only. The command-text
guard `guard-primary-checkout-write.py` blocks the writes it can read in a
shell command, but not what a launched script does: a script that `cd`s into
the primary checkout and then redirects to a relative path writes there unseen.

## The write guard's contract: an accident tripwire

`guard-primary-checkout-write.py` is an **accident tripwire, not a containment
boundary** (decision GUARD-TRIPWIRE-1113, 2026-11-13, issue #403). It stops
honest mistakes that an agent makes with ordinary commands. It does not stop a
process that tries to write the primary checkout.

**What it promises to stop** (the denominator). While the primary checkout is
on a protected branch, the guard refuses:

1. a write tool (`Write`, `Edit`, `MultiEdit`, `apply_patch`, the AGY file
   tools) whose target is a protected file in the primary checkout;
2. a shell command that edits, writes, creates, removes, moves, copies, links
   or changes the mode of a file in the primary checkout through the writers
   and redirections the hook lists, by a literal path, a known variable, or a
   relative path from a working directory in the primary checkout;
3. a Git command that writes the primary working tree or its index (staging,
   applying patches, restoring paths from a revision, moving or removing
   tracked files, cleaning, destructive resets, popping a stash);
4. the same writes reached through ordinary launchers: command wrappers,
   `xargs` and `parallel`, `find` templates, shells given a command string,
   decidable `eval` strings, and a shell, launcher or writer run under another
   name or path. A word whose executable (followed through links, or a copy
   with identical content) is one of those programs under a different name is
   refused, whatever it runs, even when the name it uses is one the guard also
   reads;
5. the pattern classes approved under #403 (PR #412): a pattern that can
   expand to a protected path, a command chosen by a pattern, a pattern that
   can become an option, and any pattern or undecided word in a command that
   launches commands, unless every candidate provably stays outside the
   checkout;
6. any write target it cannot resolve: an unknown variable, an exhausted
   parser depth, or ANSI-C quoting.

A command in this list that the guard admits is a defect, and a review treats
it as a blocker. An input being unusual or deliberate does not by itself make
a finding out of scope; reviewers classify each finding by what it can reach
and what it can damage.

**What must keep working** (benign controls). Apart from the accepted
over-refusals below, the guard must not refuse:

- read-only commands anywhere, the primary checkout included (`git status`,
  `git log`, `git diff`, `rg`, `cat`, `ls`);
- writes inside a dispatch worktree or any other registered worktree, under
  temporary directories, or to ignored runtime state;
- the rescue forms `git checkout -- <path>` and plain `git restore <path>`;
- the commands in the pinned benign corpus
  (`tests/agents_extensions/test_guard_benign_corpus.py`).

**Accepted over-refusals.** These refuse even when the command only reads or
stays in a worktree:

- from the primary checkout, any command that holds a launcher and a pattern;
- anywhere, a command that holds a launcher and a pattern or other undecided
  word, when a launcher runs code (a shell, `eval`, `sudo` and the like, or a
  replacement string that can stand for the program), when a word could change
  program lookup or word splitting, or when a pattern could name an option, a
  launcher or an executable. A launcher counts wherever its name appears in the
  command, even as an operand;
- anywhere, a word that runs a shell, launcher or writer under another name
  (one shell under another shell's name, and a multi-call program's own
  installed file, are not renamed), and any command that sets `PATH` with an
  entry the guard cannot know, other than the inherited `$PATH`;
- anywhere, a root-changing launcher (`chroot`, `unshare`, `nsenter`, `bwrap`,
  `proot` and the like), and a relative path in a command run by
  `find -execdir`/`-okdir` or `env --chdir`, because the program it names is
  never the one the guard can see.

**What it does not stop** (the residual). A process holding sufficient
filesystem write authority can still write the primary checkout. Known ways
the command text does not show the write include shell code read from a
here-document, a file or standard input; program text given to interpreters
and filters such as `awk`, `sed` scripts, `perl` or `python`; a link created
earlier in the same command; scripts run by path and modified copies of
programs, which the guard cannot identify; and changes to the filesystem
after the guard has decided. Each review round of #403 found a
narrower case of this kind. A parser of shell text cannot close them, so the
guard does not claim to.

Real containment is an operating-system control: the primary checkout made
read-only for agent sessions. It is tracked as the containment follow-up,
owned by claude-cto. Until it lands, this tripwire, the audit hook below and
physical worktree isolation are the layers in place.

## The audit hook

`agents_extensions/shared/hooks/audit-primary-checkout-dirt.py` wraps every
shell call:

- **Before the call** (PreToolUse) it records the `git status
  --porcelain=v1 -z --untracked-files=all` entries of each protected primary
  checkout that owns the call's cwd or the project directory.
- **After the call** (PostToolUse; on Claude also PostToolUseFailure, where a
  non-zero exit goes) it compares again. A path that is dirty now and was not
  dirty before is attributed to the call. The hook writes an attribution
  record and exits 2, so the agent sees the new paths, the command, the
  record id and the exact cleanup command.

What it does not do:

- It blocks nothing. The call has already run, and a failing snapshot never
  stalls a session. An internal error after the call exits 1, which the
  harness shows as a hook error. When the pre-call snapshot is missing, a
  dirty primary is reported as "not audited" rather than attributed.
- Paths that were already dirty before the call are never attributed to it.
- Git-ignored paths never appear, because `git status` omits them.
- The probe sets `GIT_OPTIONAL_LOCKS=0`, so it never rewrites the primary
  index.

Wiring:

- Claude: `agents_extensions/shared/settings.json`, `Bash` matcher on
  PreToolUse, PostToolUse and PostToolUseFailure. These hooks are fail-open
  (no `onFailure`).
- Codex: `lu_machinery/agent_runtime/codex_hook_policy.py` takes the snapshot
  only after every PreToolUse guard has allowed the call.
  `lu_machinery/agent_runtime/codex_hook_entry.sh post-tool-use` runs the
  comparison for `Bash`. Codex runs PostToolUse after non-zero exits too, and
  its exit 2 replaces the tool result with the report.
- Dispatched Claude workers are not wired: their settings carry PreToolUse
  guards only, and they write in their own worktree.

## Cost

Each shell call runs two status probes. On the full repository (about 33,000
tracked files, clean tree), one probe measured 0.13–0.15 s and about 16 MB
RSS. The whole hook, interpreter start-up included, measured a median of
274 ms (max 329 ms) before the call and 198 ms (max 235 ms) after it, over 20
calls at load average 10 on 16 CPUs. On Claude the pre-call phase runs in
parallel with the PreToolUse guards. Each git probe has a 3 s deadline. A status listing more than 20,000
entries makes the call unauditable rather than slow. Only new untracked files
are fingerprinted: at most 200 per call, and files are hashed only up to
8 MiB.

## Removing your own files

The report prints the command. Run the dry run first, then apply:

```bash
.venv/bin/python -m lu_machinery.hygiene.primary_dirt_cleanup --session '<session>' --record '<record>'
.venv/bin/python -m lu_machinery.hygiene.primary_dirt_cleanup --session '<session>' --record '<record>' --apply
```

The command removes a file only when all of these hold:

- the session's record exists and names a root that is still a protected
  primary checkout;
- the file was untracked when recorded and is untracked now;
- its path is relative, contains no `..` or `.git` component and passes
  through no symlink (`O_NOFOLLOW` at every step);
- it is a regular file whose device, inode, size, mtime and SHA-256 match the
  record;
- no other session's record claims the same file.

Everything else is refused and reported in the JSON output:

| Outcome | Meaning |
| --- | --- |
| `refused:tracked_entry` | a tracked change, which this command never touches |
| `refused:tracked` | the file became tracked after it was recorded |
| `refused:modified_since_attribution` | the file changed after it was recorded |
| `refused:claimed_by_other_session` | another session's record claims the file |
| `refused:symlink`, `refused:symlink_or_non_directory_in_path` | a symlink on the path |
| `refused:invalid_path`, `refused:primary_root_invalid` | a forged or out-of-root record |
| `refused:not_untracked`, `refused:not_a_fingerprinted_regular_file` | not plain untracked file content |

Exit codes: 0 means every entry was removable, removed or already absent; 1
means there is no matching record and nothing was done; 2 means at least one
entry was refused.

With `--apply`, the command appends one receipt per entry to `cleanup.jsonl`.
It never removes directories: Git does not report empty directories, so they
do not dirty the checkout.

Tracked changes need a deliberate restore, for example `git checkout --
<path>`; the primary-checkout guard allows that rescue form. Make sure nobody
else made the change first. Anything the command refuses still needs the
operator, as before.

## State

Records, snapshots, the `audit.jsonl` log and `cleanup.jsonl` receipts live in
`$XDG_STATE_HOME/learn-ukrainian/primary-dirt-audit/` (default
`~/.local/state/...`), outside the repository. Directories are mode `0700` and
files `0600`. Snapshots whose post phase never ran (denied or cancelled calls)
are deleted after 6 hours. Records are kept as the audit trail.

## Threat model

This is a safety net for honest-agent mistakes, plus attribution. A process
running as the same user can bypass it. It can skip the hooks, write between
two snapshots, or edit the state directory. Session binding is by knowledge of
the session id printed only to that session. It is not authentication.
Attribution is temporal: a path that appears while two calls overlap is
recorded for both, so the cleanup command refuses a file that another
session's record also claims.
