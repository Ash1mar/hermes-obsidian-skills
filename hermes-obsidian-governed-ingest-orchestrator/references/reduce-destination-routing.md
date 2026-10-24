# Knowledge destination routing

Native planning and saved page bodies retain their exact logical topic paths,
identities, candidate membership and input hashes. At typed global submission,
new destinations beneath an existing knowledge root retain that root. Other safe
relative Markdown topic paths are placed under `30_Cards/`, preserving the entire
topic path. Root spelling is canonicalized for new pages only.

This is a storage contract, not a semantic rename or regrouping. Existing page
offers must retain their exact registered destination; they are never moved by
this rule. Absolute paths, traversal and non-Markdown outputs are rejected.
Both planning acceptance and submission check the resulting physical paths for
collisions, including collisions with unrelated registered identities. A collision
requires a native correction; the program never chooses a suffix or merges pages.
Authored references are retained as well: relative sibling links may move together,
but a relative source link or qualified wiki link whose destination would change
blocks submission until a native correction. The program never rewrites links.

The original native plan and authored text remain immutable. After a successful
formal submission, a bound `knowledge-destination-routing/v1` receipt records the
logical-to-physical map, identity, native content hash and reduction identity.
This transformation does not change citation authority, accept a coverage gap,
release a pause or invalidate unaffected native bodies. Formal stage acceptance
still requires its normal contracts and `checkpoint_1` pause.
