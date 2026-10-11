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

## D-016: Header parser behavior
Date: 2026-10-11
Status: Accepted
Context: Spec defines mandatory and optional header fields. Exploration
showed folded To/Cc, missing To in ~4% of files, Bcc mirroring Cc,
placeholder addresses, and dates that all carry numeric offsets.
Decision:
1. Missing Message-ID, From, To, Subject header, or Date fails the
   email with a ParseError naming the field.
2. To header present but blank counts as missing and fails.
3. Subject: missing header fails; blank value is stored as "".
4. Bcc is stored as-is, even when it mirrors Cc.
5. Placeholder addresses (e.g. no_address@enron.com) are kept as
   normal addresses.
6. Repeated addresses within one header are kept; position tells
   them apart.
7. All addresses are lowercased and stripped.
8. Recipient strings with no @ are kept raw. A From value with no @
   fails the email.
9. Date: use the numeric offset if present, else map the timezone
   abbreviation (PST, PDT, MST, MDT, CST, CDT, EST, EDT, GMT, UT, UTC),
   else fail. Stored as ISO 8601 UTC with Z; the original header is kept
   in date_raw.
10. Optional X-headers and Content-Type: None when missing or blank.
11. Re:/Fwd: prefixes are kept in subject exactly as written.
Consequences: Failures are explicit and logged, never guessed. Drafts
and calendar items without To land in the error log. Dedup gets
lowercased addresses for blocking.

## D-017: Header parser amendments
Date: 2026-10-11
Status: Accepted
Context: Test review of D-016 and a grep of real data.
Decision:
1. Failure reasons use fixed formats: "missing <field>",
   "invalid <field>: <detail>", "empty input".
2. All recipient values are lowercased, including ones without @.
3. Input with no blank line is headers only with an empty body, not a
   failure. Supersedes the header-only case in D-016 test planning.
4. LDAP-style recipients ("Last, First </O=ENRON/...>") appear in To
   and Cc. The bracketed DN is stored, lowercased, as the address. An
   unquoted comma inside a display name does not split the recipient.
Consequences: Stats group failures cleanly. Real Enron recipient
formats parse into one recipient each.

## D-013: Final mailbox set
Date: 2026-10-11
Status: Accepted
Context: D-010 and D-011 defined stratified selection with greedy edge
case coverage. SELECTION.md produced three candidate sets.
Decision: Set 1: ring-a, ybarbo-p, presto-k, baughman-d, beck-s. One
mailbox per size band (706 to 11,830 files), 18,791 files, about
18,165 estimated parsed, 22 of 23 checklist items covered. Chosen over
Set 2 and Set 3 because it has the most real resends (68 Type C
groups), which is what duplicate detection targets, while still having
164 cross-mailbox storage-copy groups to exercise copy collapse.
Consequences: beck-s is about 60% of volume. The README explains the
selection method and this tradeoff.

## D-018: db, discover, and ingest behavior
Date: 2026-10-11
Status: Accepted
Decision:
1. db: each pipeline run deletes and rebuilds the database from
   schema.sql. connect() always sets PRAGMA foreign_keys = ON.
   insert_email writes the email and its recipients in one transaction
   and raises on any constraint error. db converts datetimes to ISO
   8601 UTC text with Z, bools to 0/1, enums to plain strings.
2. discover: takes a maildir root and a list of mailboxes, yields
   relative paths like "maildir/beck-s/inbox/1." in sorted order, files
   only, skipping hidden files. If any mailbox folder is missing, it
   raises before yielding anything, naming every missing mailbox.
3. ingest: wires discover, parse_headers, a body parser, and db. The
   body parser is passed in as a function, called with the same
   arguments as parse_body's signature in module-layout.md, so ingest
   is testable without parse_body. ParseError becomes a failure with
   its reason. Any other exception becomes
   "unexpected <ExceptionType>: <message>". Failures go to
   error_log.txt ("<source_file> | <reason>") and parse_failures. The
   pipeline never crashes on a bad file.
4. Extraction stats: total files found, parsed, failed, failures by
   reason, and per-field completeness (percent non-null and non-empty)
   for every field in Section 3 of the spec. Printed to console and
   saved as output/extraction_stats.json.
5. config: holds the D-013 mailbox list and default paths.
Consequences: Runs are reproducible. ingest can be built before
parse_body exists.

## D-019: Body parser behavior
Date: 2026-10-11
Status: Accepted
Context: Spec wants body separated from forwarded and quoted content,
plus has_attachment and headings. Exploration found quoted-printable
bodies, Outlook "-----Original Message-----" replies, Lotus Notes
"Forwarded by" blocks and inline reply headers, nested chains, and
"<<file>>" attachment references. Attachments were stripped from the
corpus.
Decision:
1. Decode quoted-printable only when Content-Transfer-Encoding says so.
2. Normalize line endings to \n.
3. body is everything before the first reply or forward marker.
   Everything from that marker on goes into one bucket, nested chains
   included.
4. The marker decides the bucket. "-----Original Message-----", Lotus
   Notes inline reply headers (name line, date line, then To:), and
   lines starting with ">" go to quoted_content. "Forwarded by" and
   "----- Forwarded Message" style blocks go to forwarded_content.
5. has_attachment is true if any part has a "<<...>>" reference, the
   Content-Type is multipart, or the main body mentions "attached" or
   "attachment" (case-insensitive, whole word).
6. headings, main body only. A line of 60 chars or less, not ending in
   a period, that is either all caps with at least one word of 3+
   letters, or followed by a line of --- or ===, or ends in ":" with 5
   words or fewer and is not the first non-empty line. Stored joined by
   \n, None if none.
7. Signatures and disclaimers stay in the body.
8. Trailing whitespace stripped from body and buckets. Empty body is
   "". Empty buckets are None.
Consequences: Dedup compares main bodies without quoted chains or QP
junk. Heading detection is a heuristic and documented as such.

## D-020: Body parser amendments (Notes header shapes, control characters)
Date: 2026-10-11
Status: Accepted
Context: A real-data check of D-019 on beck-s, presto-k and ring-a (14,352
parsed files) found 1,079 files (7.5%) where a reply header block stayed
in the main body, producing junk headings ("cc:" in 720 files). Lotus
Notes uses more header shapes than D-019 item 4 defines. The check also
found control characters in 476 quoted-printable bodies (the source writes
apostrophes as =01), "FYI" as a heading in 91 files, and 35 files whose
only attachment word was the plural.
Decision: amends D-019 items 4, 5 and 6, and adds item 9.
1. More Lotus Notes reply headers count as quoted markers, in addition to
   D-019 item 4. In every shape the marker is the first line of the
   header, and a "To:" line must follow it, after any blank lines and
   optional "Sent by:" or "Please respond to" lines.
   a. Name line, date line, To:. The date may also be 24-hour with no
      AM/PM ("27/10/2000 21:34"). Only the digit shape is checked, so
      dd/mm and mm/dd both match.
   b. "From: <name> <date>", with the date at the end of the same line.
   c. "<name> on <date>", with the date at the end of the same line.
   d. A "To:" line with a blank line (or the start of the body) above it,
      followed by a "cc:" or "Subject:" line. The To: list may wrap onto
      continuation lines. The marker is the To: line itself.
2. Only shapes covering at least 20 files were added. Counts are for the
   three mailboxes above, before this change. Known gaps, not handled:
   - Outlook style "From:/Sent:/To:/Subject:" block with no
     "-----Original Message-----" line (10 files).
   - "--------- Inline attachment follows ---------" header blocks, mostly
     calendar items (9 files).
   - A long tail of shapes with fewer than 20 files each, for example a
     date wrapped onto the next line, or a trailing token after the date
     (about 4 files each).
3. D-019 item 6, all-caps rule: the word must have 4 or more letters (was
   3), so "FYI" and "NDA" are not headings.
4. D-019 item 5: "attachments" also counts as a whole word, next to
   "attached" and "attachment".
5. New item 9: after decoding, control characters are stripped from body
   and buckets, except \n and \t. That means U+0000 to U+001F and U+007F.
   U+0080 to U+009F are left alone (none occur in the three mailboxes, and
   in Latin-1 decoded text they are usually mis-decoded quotes).
Consequences: Fewer reply chains leak into the main body, so dedup compares
cleaner bodies and fewer header lines become headings. Stripping =01
apostrophes loses the character ("Int=01,l" becomes "Int,l"). The gaps above
are visible, not hidden. Heading detection is still a heuristic.
