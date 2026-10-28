# Revocable sealed evidence reads

This opt-in Linux implementation reduces file-content reads across history
queries, response authentications and Build send checks. A separately launched
privileged protector owns revocation. Native consumers irreversibly remove all
Linux capabilities, their capability bounding set, and set `no_new_privs` before
using a snapshot. This is a change in the storage/write contract, not ordinary
time-based caching. Existing task/input/history fingerprints are unchanged.

## Protected and dynamic data

The manifest covers completed task and slice records, Pass files, their enumerated
directories, and exact recovery reports named by current history pointers. The
protector requires the accepted upstream checkpoint and stopped Build stage. It
accepts completed slices and terminal cancelled historical amendment descriptors
with no leases; cancelled descriptors are preserved dependencies, never accepted
semantic results. Ready, failed, running or leased slices cannot be sealed.
Originals are not copied. Schema
and code remain bound by deployment quiescence, reader hash and fresh schema
checks. Workflow, batch, journals, audits, native transcripts, attempts, allowances
and quota records stay mutable and use the existing fresh reads. In particular,
the first version deliberately does not reduce full-workflow reads for pointers.

Every protected file and enumerated directory receives `FS_IMMUTABLE_FL`. Registry
state, manifest and its directory are also protected. The ext4 protection includes
membership operations and ordinary root writes; consumers cannot clear the flag,
gain capabilities through exec, mount over paths, or regain privileges. Metadata
checks cover every original path component and sealed inode, detecting path and
symlink replacement without rereading file contents. This is not permission to
cache by mtime/size: the kernel seal supplies the write exclusion. The additional
inode/ctime checks detect administrative tampering, but privileged kernel or raw
device modification is outside the ordinary-writer contract. Privileged manual
maintenance must stop consumers first.

Kernel basis: [ext4 immutable flag transition](https://github.com/torvalds/linux/blob/v6.6/fs/ext4/ioctl.c#L585-L625).
Actual host behavior, including an already-open writable descriptor, must pass
the isolated postdeployment checks before any real snapshot is enabled.

## State and revocation

`preparing` is durable before sealing. The candidate includes original inode flags
for crash recovery, hashes, membership, static workflow binding, reader version,
and a unique generation. After sealing, the protector completely rehashes the
files and rechecks membership. Only then may it publish `active`. The active
registry is bound to a live protector PID/start identity; a dead process, missing
record, changed generation or unprotected registry never authorizes reuse.

Each original gate remains in place and reads current workflow controls. History
queries additionally check the small current registry and protected inode path
before using their detached indexed data. Graph and send checks check the whole
sealed manifest's metadata plus fresh batch/schema bytes. The first content read
in each consumer verifies the original hash; later reads reuse those verified
bytes while the same generation and kernel protection remain valid.

The protector alone clears kernel flags. Atomic writers request revocation
before writing a protected target, then fail without mutation so controlled
maintenance can proceed after consumers stop. Direct file or membership writers
cannot bypass the seal. No RPC accepts file data, shell commands or permission
grants. The protected-write RPC can only request revocation. A short shared kernel
lease protects each pure read and full write-locked commit; no lease spans a model
call. Revocation takes its exclusive lease, durably publishes `revoked`, and only
then restores the original flags. The acknowledgement is the revocation's
linearization point: an already-running atomic commit finishes before it, and no
new consumer can use the old generation after it. A raw model return and its
audit may still be preserved; a subsequent domain commit must pass the current
gate and cannot use the revoked generation.

Normal protector shutdown revokes and releases flags. After a protector crash,
the old active marker is unusable; quiesce all consumers and run `release-stale`
before controlled repair. It persists revocation before restoring recorded flags.
Preparing failures release their partial seals. Reopening always means a new
generation and full verification. A restarted consumer always reads and hashes
its own first captured bytes; disk flags alone are not a remembered result.

## Operations

Only stopped, explicitly authorized Build scopes may launch the protector after
supported recovery and before resume. Check that no other workflow or source
writer can use the affected directories. Record previous/candidate SHAs and
normal installation checks first. Use the installed Skill path:

```sh
python3 "<skill-dir>/scripts/manage_evidence_snapshot.py" serve --vault "<vault>" --workflow-id "<workflow>"
python3 "<skill-dir>/scripts/manage_evidence_snapshot.py" status --vault "<vault>" --workflow-id "<workflow>"
python3 "<skill-dir>/scripts/manage_evidence_snapshot.py" revoke --vault "<vault>" --workflow-id "<workflow>" --reason "controlled maintenance"
```

The server is a separately owned process, not a domain worker. Resume is a
separate supported operator action. At the observation deadline cancel the
workflow with the stable cancellation identity, verify drain, then revoke/release
the protector. Snapshot metadata are audit/control data, not domain acceptance.

## Verification and measurement

Isolated Linux checks cover blocked writes through normal and existing file
descriptors, unlink/rename/new members/symlinks, capability regain prevention,
ordered/duplicate history choices, workflow cancellation without revision change,
legal revocation, failed/missing registry, crashed protector, privileged same-mtime
tampering, ancestor symlink replacement, and full commit reads. Existing request,
budget, source, identity and recovery-chain tests remain in scope.

Report preparation sealing/verification bytes separately from each consumer's
first reads and repeated snapshot hits. `evidence_snapshot_summary` attributes
first/hit/avoided bytes by original read origin; those are logical application
bytes, not physical disk traffic. `snapshot_dependency_reuse` and
`snapshot_dependency_recheck` identify avoided full scans. Full commit verification
uses `full_evidence_reads` and retains content reads. A same-history read-only
comparison must show identical selection and fingerprint results, with the
baseline and snapshot bytes independently counted. Network failures remain
execution observations, never valid conclusions or evidence of a cache defect.
The legacy JSON-cache `bytes` counter describes bytes presented to its derivation
path, including protected in-memory inputs. Use `logical_read_sources` for actual
application reads and the protector/session counters for first reads and avoided
bytes; never add reused inputs to actual content I/O.
