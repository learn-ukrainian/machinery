"""Pure argv classifier for the primary Git policy (#208, #29).

Ported from the reviewed #216 lineage. Shell expansion belongs to the caller;
this module receives Git's actual argument words and never parses shell text.
"""

from __future__ import annotations


def _names(text: str) -> frozenset[str]:
    return frozenset(text.split())


_GIT_SAFE_GLOBAL_FLAGS = _names(
    """
    --no-pager -P --no-optional-locks --literal-pathspecs --glob-pathspecs --noglob-pathspecs --icase-pathspecs
    --no-replace-objects
    """
)
_GIT_TARGET_GLOBALS = ("-C", "--git-dir", "--work-tree")
# Informational invocations without a subcommand.
_GIT_INFO_GLOBALS = _names("--version -v --help -h --exec-path --html-path --man-path --info-path")

# Revision, history and diff options shared by the history readers.
_GIT_HISTORY_LONG = _names(
    """
    oneline format pretty abbrev-commit no-abbrev-commit abbrev no-abbrev graph all decorate no-decorate
    decorate-refs decorate-refs-exclude clear-decorations source stat shortstat numstat dirstat dirstat-by-file
    cumulative compact-summary summary name-only name-status raw patch no-patch patch-with-stat patch-with-raw
    unified minimal patience histogram diff-algorithm anchored word-diff word-diff-regex color-words color
    no-color color-moved no-color-moved color-moved-ws no-color-moved-ws no-renames renames find-renames
    find-copies find-copies-harder break-rewrites irreversible-delete diff-filter check full-index binary
    relative no-relative text ignore-space-at-eol ignore-space-change ignore-all-space ignore-blank-lines
    ignore-cr-at-eol ignore-matching-lines inter-hunk-context function-context exit-code quiet no-ext-diff
    no-textconv ignore-submodules submodule src-prefix dst-prefix no-prefix default-prefix line-prefix
    ita-invisible-in-index ita-visible-in-index cached staged merge-base no-index indent-heuristic
    no-indent-heuristic ws-error-highlight stat-width stat-name-width stat-graph-width stat-count skip-to
    rotate-to follow no-follow max-count skip since after until before since-as-filter author committer grep
    all-match invert-grep regexp-ignore-case basic-regexp extended-regexp fixed-strings perl-regexp remove-empty
    merges no-merges min-parents max-parents no-min-parents no-max-parents first-parent exclude-first-parent-only
    not branches tags remotes glob exclude reflog single-worktree ignore-missing cherry-mark cherry-pick
    left-only right-only cherry walk-reflogs merge boundary simplify-by-decoration show-pulls full-history dense
    sparse simplify-merges ancestry-path date-order author-date-order topo-order reverse no-walk do-walk date
    relative-date parents children left-right cc dd diff-merges no-diff-merges combined-all-paths remerge-diff
    show-linear-break encoding expand-tabs no-expand-tabs notes no-notes show-notes standard-notes
    no-standard-notes log-size mailmap use-mailmap no-mailmap no-use-mailmap full-diff count objects objects-edge
    no-object-names timestamp pickaxe-all pickaxe-regex find-object base ours theirs creation-factor dual-color
    no-dual-color numbered email group include-untracked only-untracked i-still-use-this
    """
)
_GIT_HISTORY_SHORT = "pusczwbRaWtrmvqiEFPgeD"
_GIT_HISTORY_VALUED = "nUMCBlSGLI"

# (long options, short flags, short options whose remainder is a value, numeric `-<n>`)
_GitSpec = tuple[frozenset[str], str, str, bool]
_GIT_HISTORY: _GitSpec = (_GIT_HISTORY_LONG, _GIT_HISTORY_SHORT, _GIT_HISTORY_VALUED, True)
_GIT_READERS: dict[str, _GitSpec] = {
    **dict.fromkeys(
        ("log", "show", "diff", "diff-tree", "diff-index", "diff-files", "whatchanged", "shortlog", "rev-list",
         "range-diff", "cherry"),
        _GIT_HISTORY,
    ),
    "status": (
        _names(
            """
            short branch porcelain long verbose untracked-files ignored ignore-submodules null column no-column
            ahead-behind no-ahead-behind renames no-renames find-renames show-stash
            """
        ),
        "sbvz",
        "u",
        False,
    ),
    "grep": (
        _names(
            """
            cached untracked no-index exclude-standard no-exclude-standard recurse-submodules text ignore-case
            word-regexp invert-match full-name extended-regexp basic-regexp perl-regexp fixed-strings line-number
            column files-with-matches name-only files-without-match null count color no-color break heading
            show-function function-context after-context before-context context threads max-depth only-matching
            quiet all-match and or not max-count recursive no-recursive
            """
        ),
        "aiIvwhHEGPFnlLzcoqWpr",
        "ABCemf",
        True,
    ),
    **dict.fromkeys(
        ("blame", "annotate"),
        (
            _names(
                """
                porcelain line-porcelain incremental root show-stats score-debug show-name show-number show-email
                abbrev date reverse first-parent encoding progress no-progress ignore-rev ignore-revs-file
                color-lines color-by-age minimal
                """
            ),
            "bclfnpstwe",
            "LMC",
            False,
        ),
    ),
    "ls-files": (
        _names(
            """
            cached deleted modified others ignored stage directory no-empty-directory unmerged killed exclude
            exclude-from exclude-per-directory exclude-standard error-unmatch with-tree full-name
            recurse-submodules abbrev debug eol deduplicate sparse format resolve-undo
            """
        ),
        "cdmoiskuzvtf",
        "xX",
        False,
    ),
    "ls-tree": (_names("long name-only name-status object-only full-name full-tree abbrev format"), "dlrtz", "", False),
    "cat-file": (
        _names(
            """
            batch batch-check batch-command batch-all-objects buffer unordered follow-symlinks allow-unknown-type
            use-mailmap mailmap no-mailmap no-use-mailmap
            """
        ),
        "tspe",
        "",
        False,
    ),
    "rev-parse": (
        _names(
            """
            abbrev-ref short verify quiet show-toplevel show-prefix show-cdup git-dir git-common-dir
            absolute-git-dir is-inside-work-tree is-inside-git-dir is-bare-repository is-shallow-repository
            symbolic symbolic-full-name path-format git-path show-object-format show-ref-format default revs-only
            no-revs flags no-flags sq sq-quote local-env-vars all branches tags remotes glob exclude disambiguate
            since after until before resolve-git-dir show-superproject-working-tree shared-index-path
            """
        ),
        "q",
        "",
        False,
    ),
    "merge-base": (_names("all octopus independent is-ancestor fork-point"), "a", "", False),
    "for-each-ref": (
        _names(
            """
            count sort format color shell perl python tcl points-at merged no-merged contains no-contains
            ignore-case omit-empty exclude include-root-refs
            """
        ),
        "sp",
        "",
        False,
    ),
    "show-ref": (_names("head heads tags branches verify dereference hash abbrev quiet exists"), "dsq", "", False),
    "name-rev": (_names("tags refs exclude all name-only no-undefined always"), "", "", False),
    "describe": (
        _names(
            "dirty broken all tags contains abbrev candidates exact-match debug long match exclude always first-parent"
        ),
        "",
        "",
        False,
    ),
    "check-ignore": (_names("quiet verbose null non-matching no-index"), "qvzn", "", False),
    "check-attr": (_names("all cached null source"), "az", "", False),
    "check-ref-format": (_names("normalize allow-onelevel no-allow-onelevel refspec-pattern branch"), "", "", False),
    "count-objects": (_names("verbose human-readable"), "vH", "", False),
    "var": (frozenset(), "l", "", False),
    "version": (_names("build-options"), "", "", False),
    "ls-remote": (_names("heads tags refs branches quiet exit-code get-url symref sort"), "htqb", "", False),
    "show-branch": (
        _names(
            """
            all remotes topo-order date-order current sparse more list merge-base independent no-name sha1-name
            topics reflog color no-color
            """
        ),
        "ar",
        "",
        False,
    ),
    "help": (
        _names("all guides config user-interfaces developer-interfaces verbose no-verbose external-commands aliases"),
        "agc",
        "",
        False,
    ),
}
# Commands whose action words and operands decide whether they read are parsed
# the way Git's parse-options reads them: a value-taking option consumes its
# value (inline, the rest of a short cluster, or the next word), so a value is
# never mistaken for a dry-run flag or a read action.
_GitOptions = tuple[frozenset[str], frozenset[str], frozenset[str]]  # flags, valued, next-word-unless-option


def _opts(flags: str, valued: str = "", lastarg: str = "") -> _GitOptions:
    return _names(flags), _names(valued), _names(lastarg)


_GIT_BRANCH_FILTERS = "--contains --no-contains --merged --no-merged"
_GIT_BRANCH_LIST = _opts(
    "-l --list -a --all -r --remotes -v --verbose -i --ignore-case --show-current --omit-empty --abbrev "
    + "--no-abbrev --column --no-column --color --no-color",
    "--sort --format --points-at",
    _GIT_BRANCH_FILTERS,
)
_GIT_BRANCH_DELETE = _opts("-d -D --delete -q --quiet")
_GIT_TAG_LIST = _opts(
    "-l --list -n -i --ignore-case --omit-empty --column --no-column --color --no-color",
    "--sort --format --points-at",
    _GIT_BRANCH_FILTERS,
)
_GIT_LIST_MODE = _names("-l --list -n --points-at " + _GIT_BRANCH_FILTERS)
_GIT_REMOTE = _opts("-v --verbose --push --all -n")
_GIT_CONFIG = _opts(
    "--get --get-all --get-regexp --get-urlmatch --list -l --global --system --local --worktree --show-origin "
    + "--show-scope --name-only --null -z --bool --int --bool-or-int --path --expiry-date --includes --no-includes "
    + "--all --regexp --fixed-value",
    "--file -f --blob --type --default --url --value",
)
_GIT_CONFIG_ACTIONS = _names("--get --get-all --get-regexp --get-urlmatch --list -l")
_GIT_CONFIG_WRITE_WORDS = _names("set unset rename-section remove-section edit get list")
_GIT_CLEAN = _opts("-n --dry-run -d -x -X -q --quiet -f --force", "-e --exclude")
_GIT_WORKTREE_LIST = _opts("--porcelain -v --verbose -z", "--expire")
_GIT_WORKTREE_ADD = _opts(
    "--detach -d -q --quiet --lock --checkout --no-checkout --track --no-track --guess-remote --no-guess-remote",
    "-b -B",
)
_GIT_SYMBOLIC_REF = _opts("-q --quiet --short --no-recurse --recurse")
_GIT_SUBMODULE_STATUS = _opts("--cached --recursive -q --quiet")
# Bounded maintenance forms on the protected route (issue #208 AC-5).
_GIT_PULL = _opts("--ff-only -q --quiet -v --verbose --no-rebase --no-edit --stat --no-stat")
_GIT_FETCH = _opts(
    "-q --quiet -v --verbose --prune -p --prune-tags -P --tags -t --no-tags -n --all --dry-run "
    + "--no-recurse-submodules"
)
_GIT_PROTECTED_BRANCHES = _names("main master HEAD")
# Optional values that only attach to the option (`tag -n5`), never the next word.
_GIT_ATTACHED_ONLY = _names("-n")


def _git_parse(args: list[str], spec: _GitOptions) -> tuple[list[tuple[str, str | None]], list[str]] | None:
    """Git parse-options view of ``args``: ``(options with values, operands)``.

    Returns ``None`` when any option is not listed in ``spec``. ``--`` ends the
    options. Every option an abbreviation or an unlisted negation would name is
    unlisted, so it is refused.
    """
    flags, valued, lastarg = spec
    options: list[tuple[str, str | None]] = []
    operands: list[str] = []
    i = 0
    while i < len(args):
        word = str(args[i])
        i += 1
        if word == "--":
            operands.extend(str(arg) for arg in args[i:])
            break
        if not word.startswith("-") or word == "-":
            operands.append(word)
            continue
        if word.startswith("--"):
            name, equals, value = word.partition("=")
            if name in valued and not equals:
                if i >= len(args):
                    return None
                value, i = str(args[i]), i + 1
            elif name in lastarg and not equals and i < len(args) and not str(args[i]).startswith("-"):
                value, i = str(args[i]), i + 1
            elif name not in flags | valued | lastarg:
                return None
            options.append((name, value if equals or name in valued | lastarg else None))
            continue
        body = word[1:]
        for j, char in enumerate(body):
            name = "-" + char
            rest = body[j + 1 :]
            if name in valued:
                if not rest:
                    if i >= len(args):
                        return None
                    rest, i = str(args[i]), i + 1
                options.append((name, rest))
                break
            if name in flags and name in _GIT_ATTACHED_ONLY and rest.isdigit():
                options.append((name, rest))
                break
            if name not in flags:
                return None
            options.append((name, None))
    return options, operands


def _git_options_listed(options: list[str], spec: _GitSpec) -> bool:
    """Whether every option word of a pure reader is an exact listed form of ``spec``.

    Pure readers have no action or operand that writes, so values are never
    skipped: a value word that starts with ``-`` is checked as an option too,
    and an unlisted word is refused, never guessed.
    """
    longs, shorts, valued, numeric = spec
    for word in options:
        if word.startswith("--"):
            if word.split("=", 1)[0][2:] not in longs:
                return False
            continue
        body = word[1:]
        if numeric and body.isdigit():
            continue
        for char in body:
            if char in valued:
                break
            if char not in shorts:
                return False
    return True


def _git_reader_words(args: list[str]) -> list[str]:
    """Option words of a pure reader (``--`` ends options)."""
    head = args[: args.index("--")] if "--" in args else args
    return [str(word) for word in head if word.startswith("-") and word != "-"]


def git_invocation_form(sub: str, args: list[str]) -> tuple[str, list[str]] | None:
    """Classify one Git subcommand and its arguments against the allowlist (#208).

    Returns ``("read", [])`` for an enumerated read-only form, a maintenance
    or rescue kind with the paths it creates or removes, or ``None`` when the
    form is not enumerated (refused on the protected route).
    """
    args = [str(word) for word in args]
    spec = _GIT_READERS.get(sub)
    if spec is not None:
        return ("read", []) if _git_options_listed(_git_reader_words(args), spec) else None
    action = args[0] if args else ""
    read: tuple[str, list[str]] = ("read", [])
    if sub == "branch":
        parsed = _git_parse(args, _GIT_BRANCH_DELETE)
        if parsed is not None and parsed[1] and any(name in {"-d", "-D", "--delete"} for name, _ in parsed[0]):
            branches = parsed[1]
            if not any(word.startswith("-") or word in _GIT_PROTECTED_BRANCHES for word in branches):
                return "branch_delete", []
            return None
        parsed = _git_parse(args, _GIT_BRANCH_LIST)
        listing = parsed is not None and (not parsed[1] or any(name in _GIT_LIST_MODE for name, _ in parsed[0]))
        return read if listing else None
    if sub == "tag":
        parsed = _git_parse(args, _GIT_TAG_LIST)
        listing = parsed is not None and (not parsed[1] or any(name in _GIT_LIST_MODE for name, _ in parsed[0]))
        return read if listing else None
    if sub == "remote":
        parsed = _git_parse(args, _GIT_REMOTE)
        if parsed is None:
            return None
        operands = parsed[1]
        shaped = not operands or (operands[0] == "get-url" and len(operands) == 2) or (
            operands[0] == "show" and len(operands) >= 2
        )
        return read if shaped else None
    if sub == "config":
        parsed = _git_parse(args, _GIT_CONFIG)
        if parsed is None:
            return None
        names, operands = {name for name, _ in parsed[0]}, parsed[1]
        if operands[:1] in (["get"], ["list"]):
            return read
        if names & _GIT_CONFIG_ACTIONS:
            return read if len(operands) <= 2 else None
        return read if len(operands) == 1 and operands[0] not in _GIT_CONFIG_WRITE_WORDS else None
    if sub == "stash":
        if action in {"list", "show"}:
            return read if _git_options_listed(_git_reader_words(args[1:]), _GIT_HISTORY) else None
        return None
    if sub == "worktree":
        if action == "list":
            parsed = _git_parse(args[1:], _GIT_WORKTREE_LIST)
            return read if parsed is not None and not parsed[1] else None
        if action == "add":
            parsed = _git_parse(args[1:], _GIT_WORKTREE_ADD)
            if parsed is None or len(parsed[1]) not in (1, 2):
                return None
            branches = [value or "" for name, value in parsed[0] if name in {"-b", "-B"}]
            if any(branch.startswith("-") or branch in _GIT_PROTECTED_BRANCHES for branch in branches):
                return None
            return "worktree", parsed[1][:1]
        if action in {"remove", "unlock"}:
            parsed = _git_parse(args[1:], _opts("-f --force" if action == "remove" else ""))
            return ("worktree", parsed[1]) if parsed is not None and len(parsed[1]) == 1 else None
        return None
    if sub == "symbolic-ref":
        parsed = _git_parse(args, _GIT_SYMBOLIC_REF)
        return read if parsed is not None and len(parsed[1]) == 1 else None
    if sub == "reflog":
        operands = [word for word in args if not word.startswith("-")]
        if not operands or action in {"show", "list", "exists"}:
            return read if _git_options_listed(_git_reader_words(args), _GIT_HISTORY) else None
        return None
    if sub == "notes":
        return read if not args or (action in {"list", "show"} and not _git_reader_words(args)) else None
    if sub == "submodule":
        if not args or action == "status":
            return read if _git_parse(args[1:], _GIT_SUBMODULE_STATUS) is not None else None
        return None
    if sub == "sparse-checkout":
        return read if args == ["list"] else None
    if sub == "clean":
        parsed = _git_parse(args, _GIT_CLEAN)
        dry_run = parsed is not None and any(name in {"-n", "--dry-run"} for name, _ in parsed[0])
        return read if dry_run else None
    if sub in {"pull", "fetch"}:
        # pull hands words after `--` to fetch, which still parses them as options.
        parsed = None if "--" in args else _git_parse(args, _GIT_PULL if sub == "pull" else _GIT_FETCH)
        if parsed is None or any(":" in word for word in parsed[1]):
            return None
        if sub == "pull" and (len(parsed[1]) > 2 or ("--ff-only", None) not in parsed[0]):
            return None
        return sub, []
    if sub == "checkout":
        # Rescue: discard dirt from the primary's own index.
        return ("rescue", []) if args[:1] == ["--"] and len(args) > 1 else None
    if sub == "restore":
        paths = args[1:] if args[:1] == ["--"] else args
        return ("rescue", []) if paths and not any(word.startswith("-") for word in paths) else None
    return None



def split_invocation(argv: list[str]) -> tuple[list[str], str, list[str], bool, list[str]]:
    """Separate Git globals without guessing an unknown option's arity.

    Globals are returned intact for Git's own target probe. The final boolean
    admits only target selectors and the enumerated safe globals on the primary.
    Ordered -C operands preserve the execution cwd independently of the worktree.
    """
    i = 0
    strict = True
    directories: list[str] = []
    valued = {"-C", "--git-dir", "--work-tree", "-c", "--config-env", "--namespace", "--exec-path"}
    while i < len(argv):
        word = argv[i]
        if word in _GIT_INFO_GLOBALS:
            if i + 1 != len(argv):
                raise ValueError("ambiguous informational invocation")
            return argv[:i], word, [], strict, directories
        if not word.startswith("-"):
            return argv[:i], word, argv[i + 1:], strict, directories
        if word in valued:
            if i + 1 >= len(argv):
                raise ValueError("missing global option value")
            if word == "-C":
                directories.append(argv[i + 1])
            strict = strict and word in _GIT_TARGET_GLOBALS
            i += 2
        elif (
            word in _GIT_SAFE_GLOBAL_FLAGS
            or (word.startswith("-C") and len(word) > 2)
            or word.startswith(("--git-dir=", "--work-tree="))
        ):
            if word.startswith("-C") and len(word) > 2:
                directories.append(word[2:])
            i += 1
        elif word.startswith(("-c", "--config-env=", "--namespace=", "--exec-path=")) or word == "--bare":
            strict = False
            i += 1
        else:
            raise ValueError("unknown Git global option")
    return argv, "", [], strict, directories
