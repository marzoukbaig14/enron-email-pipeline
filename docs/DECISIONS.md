# Decision Log

Rules:
- Append only. Never edit or delete past entries.
- To change a decision, add a new entry that says "Supersedes D-00X".

## D-001: Dev environment
Date: 2026-10-09
Decision: Use GitHub Codespaces as the dev environment.
Why: I can work from anywhere, and the setup can be rebuilt from the repo.

## D-002: AI tools
Date: 2026-10-09
Decision: Claude Code for implementation. A claude.ai Project for planning and design, with a separate chat per topic.
Why: Planning and coding stay separate, and each design topic gets its own focused chat.

## D-003: Handoff between planning and implementation
Date: 2026-10-09
Decision: Decisions from claude.ai chats go into docs/DECISIONS.md. Implementation plans go into docs/plans/. Claude Code reads those before building.
Why: The repo is the single source of truth, so Claude Code builds from written decisions instead of chat memory.

## D-004: Decision scope
Date: 2026-10-09
Decision: Structural choices (schema, approach, tradeoffs) are made in claude.ai. Small code-level choices are made in Claude Code.
Why: Big design choices get deliberate discussion, and small choices don't hold up implementation.

## D-005: AI usage tracking
Date: 2026-10-09
Decision: Run /export at the end of every Claude Code session and save it in notes/. List claude.ai chats in notes/chats.md.
Why: The assignment requires AI_USAGE.md, and these records are its raw material.

## D-006: Data
Date: 2026-10-09
Decision: The full Enron dataset is downloaded to data/maildir and gitignored. The tarball is deleted. The mailbox subset is not chosen yet.
Why: Raw data is too large to commit and must stay unmodified. Picking the subset is a separate decision.

## D-007: Spec format
Date: 2026-10-09
Decision: Convert the assignment to docs/assignment.md with pandoc.
Why: Claude Code can read Markdown but not .docx.

## D-008: Commits
Date: 2026-10-09
Decision: Commit often, with honest messages.
Why: The history should show real progress.

## D-009: Agent-driven data exploration before design
Date: 2026-10-10
Status: Accepted
Context: Parser and dedup design depend on real data quirks. Spec requires
documenting mailbox choice and rationale.
Decision: Claude Code builds scripts/explore_data.py (stdlib only) to profile
the full maildir. Outputs go to docs/exploration/ (REPORT.md,
mailbox_stats.csv, examples.json). No pipeline code until mailboxes are
chosen. Example paths per quirk are kept for use as test fixtures.
Consequences: Exploration is reproducible and reviewable. Mailbox selection
becomes a separate decision (D-010) based on the report.

## D-010: Mailbox selection method (stratified + edge case coverage)
Date: 2026-10-10
Status: Accepted
Context: Spec requires at least 5 employee mailboxes and 10,000+ emails,
with documented rationale. Exploration showed mailboxes range from 35 to
28k files and differ a lot in edge case mix (missing To, folding, dup rate).
Decision: Split all 150 mailboxes into 5 size bands (quintiles). Pick one
mailbox per band, choosing greedily to maximize coverage of a defined edge
case checklist. Estimated parsed total (after missing-To failures) must
exceed 10,000 with margin. A coverage table shows which edge cases the
chosen set includes and which it misses. Final set is decided separately
after reviewing candidate sets.
Consequences: Selection is data-driven and explainable in the README. The
pipeline is exercised on small and large mailboxes. Gaps in edge case
coverage are visible, not hidden.

## D-011: Edge case checklist built by systematic discovery
Date: 2026-10-10
Status: Accepted
Context: The first checklist came from the spec and the first scan. It may
miss unknown cases.
Decision: Run a discovery pass before final selection: value-shape census
for key headers, frequent-line mining in bodies, full parser defect
breakdown, and a random sample review. New edge case types found are added
to the checklist before selection runs. The checklist is treated as
non-exhaustive; parser failure logs and completeness stats will feed new
cases back in later.
Consequences: Selection covers more real variety. The README can explain
how edge cases were found, not just list them. Final set goes in D-012.

## D-012: Database schema design
Date: 2026-10-11
Status: Accepted
Context: Task 2 requires a normalized SQLite schema with unique
message_id, indexes on date/from_address/subject, and dedup/notification
columns. Exploration showed most bodies exist as storage copies across
folders and mailboxes, each with its own Message-ID.
Decision:
1. One row per source file, storage copies included. Reason: spec wants
   source_file per record for traceability, and we may need individual
   copies later. Copies are grouped logically, not merged.
2. Storage copies marked with a single copy_of column pointing to the
   canonical copy's message_id (NULL on the canonical row), plus a
   content_hash column used to find copies. Reason: a list-of-IDs column
   would be denormalized and hard to query.
3. Recipients in one related table recipients(email_id, address, kind)
   with kind in to/cc/bcc. Reason: meets the spec, simple. At scale,
   addresses would be split into their own table and referenced by id.
4. from_address stored as text on emails. Consistent with 3, and the
   spec names this column.
5. Dates stored as ISO 8601 UTC text. Reason: human-readable and sorts
   correctly. Original header kept in date_raw.
6. Parse failures go to error_log.txt and a parse_failures table.
   Reason: failures become queryable for stats.
7. Extra columns: mailbox, date_raw, content_hash, copy_of,
   similarity_score. source_file is UNIQUE so reruns don't double-insert.
Note: the final mailbox set, referenced as D-012 in D-011, moves to D-013.
Consequences: Schema supports the storage-copy collapse step and spec
dedup without restructuring. Address normalization is a known scale
limitation.

## D-014: Module layout, independence rules, and test structure
Date: 2026-10-11
Status: Accepted
Context: Pipeline needs extraction, storage, dedup, and notification,
built test-first, runnable with one command. Modules must be testable in
isolation.
Decision:
1. src layout: src/enron_pipeline/ with pyproject.toml, installed with
   pip install -e .
2. One module per responsibility: models, parse_headers, parse_body,
   discover, db, ingest, dedup, notify, config. main.py at repo root is
   the CLI; --send-live enables real sends.
3. Independence: modules share only models.py. Parsers are pure (no
   file, DB, or config access). Settings are passed as arguments with
   defaults. dedup receives a DB connection. notify receives a sender
   object (dry-run or live). Only ingest.py and main.py wire modules
   together. Nothing imports from scripts/.
4. Records are dataclasses, so field typos fail loudly.
5. Tests: tests/unit/ has one file per module with synthetic inputs,
   written with each module. tests/integration/ comes later with real
   fixtures.
6. Build order: models, parse_headers, parse_body, db, discover +
   ingest, dedup, notify, main. How live sends reach MCP is decided at
   the notifier.
Consequences: Each module is buildable and testable before mailbox
selection. Swapping a module (e.g. similarity metric, sender) doesn't
touch the others.

## D-015: Stricter models (enums, immutable recipients)
Date: 2026-10-11
Status: Accepted
Context: Python dataclasses don't check types at runtime. A frozen record
holding a list can still be mutated in place. Typos like kind="To" or
status="sucess" would pass silently.
Decision:
1. RecipientKind(str, Enum) with TO="to", CC="cc", BCC="bcc".
   SendStatus(str, Enum) with SENT="sent", DRY_RUN="dry_run",
   FAILED="failed". str-based so values store as plain text in SQLite
   and work on Python 3.10.
2. Recipient.kind and SendResult.status use these enums. __post_init__
   converts a valid string to the enum and raises ValueError on an
   invalid one. Case-sensitive: "To" is rejected.
3. EmailRecord.recipients is tuple[Recipient, ...] with default ().
   __post_init__ converts a list to a tuple and raises TypeError if any
   item is not a Recipient.
Consequences: Typos and in-place mutation fail loudly. Modules can pass
plain strings or enums. Frozen fields set in __post_init__ use
object.__setattr__.
