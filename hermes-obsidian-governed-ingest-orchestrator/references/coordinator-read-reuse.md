# Coordinator fresh observations and protected content reuse

## Narrow predicates

`_failed_card` and the Pass branch of `_execution_blocked` retain their original
fresh workflow observations. Each obtains a verified read-only document, never
the outer reconciliation's older value. First matching task-map entry wins.
Failure report checks, slice checks and boolean short circuits keep their order.
Only the chosen binding is copied. An incomplete slice consults revision headers
in original history/current order; coverage materials are projected lazily only
for a qualifying revision. No protected-files list is copied by these predicates.
Mutation APIs continue to return isolated, mutable full documents.

This step eliminates full copying at the two predicates, not repeated reads of
the archived storage segments. Measure those costs independently. Existing
completion-proof and native projection caches are retained.

## General protected content reader

`protected_reader.coordinator_reads` is a generic asset-role adapter to an
already active evidence manifest. It does not create permissions or seal files.
Initial reconciliation roles are `workflow_segments` and `host_files`; the latter
uses the separately verified installed-schema contract. Future protected assets
can opt in by manifest role, without filename-specific caching logic. Ordinary
unselected files keep fresh full reads and SHA-256 checks.

The coordinator cannot safely drop all its own capabilities: native process
inventory reads other processes' environments and fails closed on denial; it
also retains original writes, native launches and stopping duties. Instead one
private stdio child per reconciliation irreversibly drops effective/permitted/
bounding/ambient capabilities and sets no-new-privileges. Its handshake attests
those settings; the parent verifies them. It exposes only read and close.

Every parent read requests the original current registry/generation/protector/
lease/path/content-identity guard in that child. First content access reads and
hashes immutable bytes against the manifest. Later requests return the original
digest only; the parent reuses private immutable bytes, not a permission verdict.
Current root references and revision binding are still checked by the logical
reader. No lease spans IPC caller work or a model/native call. Dead/revoked
protectors, reader exit, changed paths and changed expected hashes fail closed;
an admitted broken reader never silently falls back. Unsealed scopes use the
original complete path. The parent remains trusted maintenance code and may not
clear protection behind active consumers.

The protocol is versioned and binds responses to reader process, generation,
request sequence and path. Frames and assets are bounded. Content, registry and
hash metrics measured in the child merge once into the parent's current ledger;
IPC payload bytes are reported separately. Child teardown emits independent
guard/metadata/IPC costs, and private pipes close on parent exit. Native stop
inventory includes the workflow-bound reader child. No new branch, result or
historical task identity is introduced.

`_mutation` disables coordinator byte reuse while loading the writable value.
The logical workflow writer also disables it for current physical-root segment
verification, under the existing write/lock protocol. Existing worker full-commit
bypass is honored as well. Neither path caches pause/cancel/budget judgments.

To cover startup recovery reconciliation, an operator may establish a verified
stopped generation before supported recovery. After recovery, quiesce/revoke it
and establish a fresh generation bound to the new history before resume. Watcher
reconciliation then admits its own restricted reader. Do not reuse an old graph
binding for the resumed worker. These are observation handoff operations, not
authority to migrate storage or retry domain results.
