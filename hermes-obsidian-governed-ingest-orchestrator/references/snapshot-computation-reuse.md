# Pure computation reuse inside sealed evidence checks

This round leaves the storage/write protocol, fresh workflow reads, guard
positions, lease duration, risk policy, request framing, historical identities
and complete write-locked submission unchanged. It optimizes computations and
allocations that previously repeated inside every fresh check.

## Metadata plans

Each EvidenceSession belongs to one fully verified manifest generation. It
caches lexical Path objects and a plan for each exact ordered input path tuple.
Plan construction performs the former ancestor expansion and set insertion once;
its recorded set order is used on every subsequent call. Expected inode fields
are detached immutable tuples. It caches no resolved path, filesystem result or
permission verdict. A different input sequence gets its own plan; a restarted
session or new generation rebuilds all plans.

Every call still performs all original absence checks in input order, then every
original identity lstat in the former order. A sealed object still receives the
second content-identity lstat; these two observations are not combined. Current
registry bytes, generation, protector PID/start identity, all three immutable
flag reads, and the short lease are checked at their original positions. Cached
paths therefore do not hide symlink replacement, appeared missing directories,
revocation or process exit.

## Graph material and proof views

Static file names, directories, hex/binary hashes, Path sorting and encoded path
framing are prepared once per session. Static records are loaded and verified
once under the current guard. Subsequent graph checks retain fresh metadata,
workflow evidence binding, batch bytes/shape and schema reads. Internal captures
use immutable mappings plus a one-entry overlay for the current batch.

The digest remains SHA-256 over the exact former stream, in Path order:
`length(path_utf8) || path_utf8 || file_sha256_bytes`. The implementation copies
the SHA state before the batch, feeds its original path framing and freshly
computed batch digest, then feeds the pre-encoded original static suffix bytes.
It never hashes a static aggregate digest as a replacement for those bytes.

Internal proof handoffs share only frozen maps, tuple directory members and
immutable strings/bytes. Each fresh schema/batch observation still creates its
small current proof envelope. The public checked_dependencies result remains
an ordinary mutable dictionary, including mutable directory lists. Its detached
copy contains new maps/lists; mutating it cannot alter the next check. Unsealed
paths retain the existing full-read and copy path. Direct graph callers likewise
receive detached mutable records and proof maps.

## Measurement and acceptance

`evidence_snapshot_summary.computation_costs` aggregates plan builds/hits and
construction time, Path object count, static/payload builds, digest computation,
metadata loop and direct lstat time/calls, absence-check time/count, current
registry attempts/time, lease acquisition wait/count and public graph copies.
`graph_reuse_summary` reports immutable internal shares, dynamic proof envelopes
and detached public proof copies/entry volume. All metrics flush once at scope
exit. They are not permission inputs.

Metadata time includes plan lookup, comparison and its lstat/absence sub-times;
registry time includes current byte/protector/flag work. Static/payload/digest
work is nested within its caller. Do not add nested totals. Direct lstat count
does not include the lstat performed inside lexists; absence checks are counted
separately, and neither counter is physical disk traffic.

Actual first file reads remain attributed by path/source/boundary. In-memory
reuse of the entire static graph set is counted in one generation-labelled
capture group with the original file-count and avoided-byte totals. This reduces
pure accounting allocations; it does not remove a filesystem observation or
move actual reads into an unmeasured origin. Per-report guards remain separate.

Isolated regressions compare the exact ordered kernel observations, preserve the
second lstat, exercise revocation/exit/replacement/absence, compare the original
digest stream with batch in every ordering position, and mutate public proof and
record returns. Existing native requests, budgets, recovery ordering, duplicate
authority and commit checks remain in scope. Performance comparison must retain
the already reduced static content reads and report each fresh guard/check count.

If the current batch has no callable unfinished pages, a user-authorized window
may start at native worker launch to observe historical authentication. Do not
rerun held pages to create model samples. Report SDK/session gaps as unavailable
when no new model request occurs, and retain same-material read-only comparisons
as a separate result.
