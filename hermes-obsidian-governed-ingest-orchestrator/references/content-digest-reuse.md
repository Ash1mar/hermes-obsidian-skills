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
