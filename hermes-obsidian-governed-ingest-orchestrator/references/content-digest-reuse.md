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
