# Independent review batching

New unconsumed presentation pins declare `independent-page-review/v1`. The
default fixed stage profile is `review-40000`: review input40000 codepoints,
body/source/planning30000. `baseline-30000` and `pages-40000` remain explicit
alternatives in `scope.execution_policy.input_budget_profile`. Input, token and
output budgets are distinct. A missing tokenizer/context limit or unknown
upstream charge is unknown, never zero. The budget is immutable after dispatch.

One or two pages may share a review call, with separate bodies, originals, QA,
scope/risks, local reference tables, image bindings and result slots. A negative
semantic verdict is a valid review conclusion; it does not authorize another
review. Only the mechanically correctable failed page may consume its one
remaining ordinary ordinal. Valid results and unknown reservations are retained
across restart, alias renumbering and regrouping. Explicit controlled revisions
retain their separate grant contract and import consumed review ordinals.

The small `review-calls/page-call-budget.json` index references immutable request,
raw response and per-page receipt files. Recover saved raw responses before new
dispatch. Known review replies require exact native transcript proof. Finalize
checks the individual receipt, current page/assessment, owning request and result
slot; one page cannot borrow its peer approval. Full and partial Build paths use
the same checks. Stopped recovery authenticates all consumed review requests and
does not grant new page ordinals. Unacknowledged outcomes remain held.

For a saved-input comparison, run only as the trusted operator:

```sh
python3 "<skill-dir>/scripts/compare_saved_reviews.py" --vault "<vault>" --workflow-id "<workflow>" --output "<external-report.json>"
```

The tool authenticates saved native requests/replies and compares the same page
contents and risk standards under30000/40000. It never dispatches a model,
changes a workflow, resets a budget or writes inside the Vault. Group counts and
offline text tokens are mechanical comparisons, not real completion or billable
usage evidence. Authorize real calls separately: acceptance requires one real
request, two independently validated conclusions and two durable receipts.
For newly pinned work, explicit-page-risks/v2 removes a lexical applicability
trigger only with identical source spans, original inspections, finding, scope,
conditions and exceptions (whitespace runs only may normalize in claim fields).
All other triggers stay. V1 receipts recompute V1; saved conclusions are immutable.
The read-only comparison inventories every original scope and its evidence hashes,
and retains the four substantive native opinions as regressions. Nonidentical text
alone is neither equivalence nor proof of a semantic difference.
Independent bodies are fixed only for untouched future pins. Each page carries
its own originals/candidates/QA/scope. Identical whole source/context/QA bundles
may be shared through an exact-hash pool; uncertain partial overlap stays local.
Image pages keep independent single slots. The page-owned small ledger, native
response capture, two-ordinal maximum, stopped recovery and publication apply to
both one and two body slots. Existing request bytes remain reconstructible.
Use `python3 "<skill-dir>/scripts/compare_saved_bodies.py" --vault <vault>
--workflow-id <id> --output <outside-vault-report>` for a mechanical 30000/40000
fixed-page comparison. Three/four body slots remain disabled until live two-slot
benefit is authorized and verified. No model/token/time gain is inferred offline.
