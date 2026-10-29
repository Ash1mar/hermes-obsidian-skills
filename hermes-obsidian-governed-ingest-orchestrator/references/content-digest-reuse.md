# Protected content digests, schema assets and bounded history reads

## Batch 1: digests belong to verified immutable bytes

`read_hashed(path, origin)` observes the same current per-file guard as
`read_bytes`. For a file covered by the active protected generation, the first
consumer read computes SHA-256, compares the manifest, and stores immutable bytes
and their actual digest in that generation's private session. Subsequent reads
still check current registry/generation, protector, lease and path identity.
Only after that guard succeeds may the original digest be returned. Each caller
still compares it with the current expected reference. A digest is never a grant.

Ordinary files and full write-locked commit verification retain fresh reads and
hashes. No mtime, mutable dictionary address or past permission decision is used
as cache identity. Parsed projections remain detached or recursively immutable.
Changed pointers still fail even when the underlying old bytes are protected.

`content_hash_sources` is aggregated by path, origin and boundary. It records
actual hash calls, bytes, nanoseconds and protected digest reuses. First-consumer
verification is labelled `sealed_first:<origin>`; protector verification remains
separate. Hash bytes include memory work and must not be called filesystem I/O.

Isolated read-only comparisons can select conservative reference paths through
`reference_reads`. They compute hashes again rather than bypassing execution
checks; there is no worker CLI/environment option granting cached authority.
Use a fixed set of native response/audit/wire identities and unchanged recovery
reports. `native_proof` is a pure evidence proof, not current worker admission;
never substitute a fake worker check to benchmark it. Do not create new model
requests, task outcomes or retries for samples.

## Batch 2: installed schema is an independent host asset

The privileged protector CLI opts in with `--protect-schema`. It accepts only
the reader's exact installed `schemas/contracts.json` under
`/root/.hermes/skills/domain`; callers cannot supply an arbitrary host path.
The manifest records its separate `hermes-installed-schema/v1` root/path/hash
contract. It is excluded from the Vault graph digest and all existing identities.

Before sealing, a protector obtains a nonblocking exclusive flock on that schema
inode and rejects any pre-existing schema/schema-directory immutable flag. This
prevents overlapping ownership and accidental restoration of another owner's
flags. A crashed owner's stale seal must be explicitly released from its recorded
manifest; no new protector silently adopts it. The file and its schema directory
are kernel immutable; all lexical ancestors are checked at each schema read.
The consumer loses all Linux capabilities as for Vault evidence.

Every original schema check still executes a current generation/protector/lease/
metadata guard and uses the original SHA-256 as its validator/proof key. Without
an opted-in schema manifest, the original full read/hash path remains. Full
locked commit verification also rereads and hashes it. The diagnostic schema
reference mode keeps the same current host guard while rereading/rehashing bytes.

Controlled writes request revocation through the owner's existing RPC, then stop
affected consumers and wait for release before replacing installed code/schema.
Revoke restores the exact original flags and releases the owner FD. Deployment
must stop all code consumers first. `release-stale` is allowed only for Vault
paths plus the exact installed schema file/schema directory; it cannot restore
arbitrary host paths from a manifest. An isolated test may pass an explicit temp
asset descriptor, but the production CLI has no such argument.

## Batch 3: bounded pure history-query steps

Only `journal_rows` iterator advancement and the internal `permitted_tasks`
report loop share a registry observation. The mutable workflow is read before
this scope at its original position. `entries` remains on per-report guards:
its caller-provided selector is arbitrary code and cannot be enclosed safely.
Unprotected/unknown reports, more than 256 pointers or over 64 MiB of report
content use the original path. Eligibility never resolves later paths or parses
later reports, preserving lazy errors and early returns.

The first original report guard acquires the shared kernel lease and checks the
current registry, generation, immutable flags and protector. Later original
report guards in that pure step check protector PID/start freshly and perform
all original path/ancestor/content-metadata checks. Expected digest comparisons,
ordering, duplicates, reverse queries and parsed-result isolation remain intact.
Steps with no report access acquire no lease or authority. Metrics distinguish
`guard_calls`, `process_checks`, `query_members`, actual `registry_attempts` and
`lease_acquires`; fewer registry reads must not be reported as fewer member or
execution checks.

Revocation has an explicit linearization point: the existing exclusive lease
waits for this bounded pure step, then durably revokes and releases protection
before acknowledging. Legal writes cannot occur during the step. A protector
death is still noticed at the next original member guard. Privileged maintenance
must stop consumers; no ordinary writer can modify protected files directly.
This changes revocation waiting granularity from one report to one pure query
step; it is not a claim of identical wall-clock observation times.

The iterator releases its lease **before each caller yield**, including early
return consumers; permission enumeration releases before returning its data.
No model call, callback, wait, current worker-authority check or entire recovery
loop is enclosed. Already captured rows keep the original lazy semantics; the
next report access after acknowledged revoke fails. Full locked-commit reads
and all current execution gates remain unchanged. Conservative diagnostic mode
can disable query sharing for fixed-material comparisons.
