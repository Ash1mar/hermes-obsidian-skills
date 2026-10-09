# Program and model boundary

New v10 workflows pin `hermes-program-worker/v1` for source preparation,
exact planning, checkpoint validators, release planning/apply, Provider sync
and final acceptance. The reconciler invokes `hermes ingest-program-dispatch`
through the installed backend plugin. Reserved non-profile assignee
`ingest-program` keeps those cards out of the conversational dispatcher.

The program executor uses native claim/run, parent checks, workspace, PID
fingerprint, restart-safe scope, heartbeat and failure bookkeeping. Its fixed
entrypoint is `python3 "<orchestrator>/scripts/run_program_worker.py" --vault
"<vault>" --binding "<canonical-binding>"`, invoked by the trusted native
dispatcher using the Hermes Python interpreter, never assembled by a model.
The card's arbitrary shell text is not executed. The installed Hermes API is
required; there is no model fallback or manual completion fallback.

Programs retain worker identity/input/template checks and supported domain
operations. The trusted reconciler acknowledges durable results and holds
successors at existing pauses. Errors preserve valid domain work, record an
execution/validation failure and hold affected work. Disabled intranet Provider
configuration never launches a command/model/index and cannot count as a
successful requested sync. Provider output must identify the exact ready
release and hash before its status is committed.

Semantic Pass, resource/global Reduce and the build-finalize page review remain
model tasks. Finalize cannot infer or manufacture review notes, page hashes,
parent hashes or semantic approvals. Program release planning uses only this
workflow's completed batch runs, not every unapplied run in the Vault.

Existing template snapshots are immutable. Installing v10 does not rewrite
old workflow pins, reassign live tasks or re-run accepted preparation, planning
or canary products. A legacy workflow keeps its pinned execution contract until
an explicitly supported contract amendment is available and authorized. While
paused, deployment and isolated checks do not authorize such an amendment,
resume, domain retry, pause continuation, publication or Provider sync.

Validate the installed native executor in an isolated board/Vault, and use only
missing/invalidated authorized real work for later stage acceptance. A program
run's lack of a model session is mechanism evidence, not a measurement of total
end-to-end cost. Track semantic calls and static context separately.
