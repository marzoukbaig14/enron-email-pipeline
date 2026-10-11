# Module layout plan

Implements D-014. Schema is D-012 (`schema.sql`). Model strictness is D-015. Status: **accepted 2026-10-11**, with one addition: ingest also catches unexpected exceptions and logs type and message (see ingest).

Package: `src/enron_pipeline/`. CLI: `main.py` at repo root. Python 3.10+.

## 1. Modules

### models.py
**Responsibility:** the shared record types. The only module every other module may import.

Contents (dataclasses, two enums and one exception, no logic beyond checking values on construction, per D-015):

| Type | Purpose |
|---|---|
| `RecipientKind(str, Enum)` | `TO="to"`, `CC="cc"`, `BCC="bcc"`. |
| `SendStatus(str, Enum)` | `SENT="sent"`, `DRY_RUN="dry_run"`, `FAILED="failed"`. |
| `Recipient(address, kind, position)` | One To/Cc/Bcc address. Frozen. `kind` is a `RecipientKind`; a valid string is converted, an invalid or wrong-case one (`"To"`) raises `ValueError`. |
| `EmailRecord` | One row of `emails` plus its recipients (section 2). `recipients` is a tuple; a list is converted, a non-`Recipient` item raises `TypeError`. |
| `ParseFailure(source_file, reason, failed_at)` | One row of `parse_failures`. |
| `ParseError(Exception)` | Raised by parsers with a short, stable `reason` string. |
| `DuplicateFlag` | One flagged duplicate. Same fields as a `duplicates_report.csv` row. Produced by dedup, consumed by notify. |
| `SendResult` | Outcome of one send attempt. Same fields as a `send_log.csv` row, plus `duplicate_message_id`. `status` is a `SendStatus`, validated like `Recipient.kind`. Produced by notify, consumed by db. |

Must NOT: read files, touch the DB, import any other package module, or do validation beyond what dataclass construction gives.

### parse_headers.py
**Responsibility:** raw bytes of one file -> parsed headers plus the raw body text. This is the only module that sees bytes, so it owns decoding.

```python
@dataclass(frozen=True, slots=True)
class ParsedHeaders:
    message_id: str
    date: datetime              # tz-aware, UTC
    date_raw: str | None
    from_address: str
    to: list[str]
    cc: list[str]
    bcc: list[str]
    subject: str                # "" if absent
    x_from: str | None
    x_to: str | None
    x_cc: str | None
    x_bcc: str | None
    x_folder: str | None
    x_origin: str | None
    content_type: str | None
    content_transfer_encoding: str | None   # for parse_body (D-019); not stored in the DB
    raw_body: str               # everything after the header block, decoded

def parse_headers(raw: bytes) -> ParsedHeaders
def parse_date(value: str) -> datetime              # PST/EST/CDT/offsets -> UTC
def parse_address_list(value: str) -> list[str]     # unfolds, splits, strips display names
```

Inputs: bytes only.
Raises `ParseError` when a mandatory header (message_id, date, from, to) is missing or unparseable. The reason names the header, e.g. `"missing To header"`.

Recipient splitting rules (`parse_address_list`, on top of D-016 and D-017):
- **Surname gluing.** An unquoted `Last, First <addr>` is one recipient. A segment is glued onto the following `First <addr>` segment only if it looks like a surname: letters, spaces, hyphens and apostrophes only, no `@` and no dot. So `Jordan, Mike </O=ENRON/...>`, `O'Neal, Shaq <s@x.com>` and `Smith-Jones, Ann <a@x.com>` are one recipient each, while `tim.belden, Mike <m@x.com>` is two.
- **Unbalanced quotes.** If the double quotes in a header do not pair up, quotes are ignored and commas are split outside `<...>` only. One stray quote therefore cannot swallow the recipients after it.

Must NOT: open files, know `source_file` or `mailbox`, split the body into forwarded/quoted parts, read config.

### parse_body.py
**Responsibility:** raw body text -> body plus the optional content fields.

```python
@dataclass(frozen=True, slots=True)
class ParsedBody:
    body: str
    forwarded_content: str | None
    quoted_content: str | None
    headings: str | None
    has_attachment: bool

def parse_body(
    raw_body: str,
    content_type: str | None = None,
    content_transfer_encoding: str | None = None,
) -> ParsedBody
def compute_content_hash(body: str) -> str          # input definition: see open question 4
```

Inputs: the decoded body string, the Content-Type header value (attachment inference, and the charset used when decoding quoted-printable) and the Content-Transfer-Encoding header value (quoted-printable is decoded only when it says so). Behavior is fixed by D-019.
Never raises on odd bodies. It returns its best effort, and an empty body is valid.

*Change from the first version of this plan (D-019): the third parameter `content_transfer_encoding` was added, and `ParsedHeaders` gained the matching field. ingest passes `parsed.content_type` and `parsed.content_transfer_encoding`.*

Must NOT: touch headers beyond the `content_type` argument, open files, touch the DB, read config.

### discover.py
**Responsibility:** find email files and name them.

```python
def discover_email_files(maildir: Path, mailboxes: Sequence[str] | None = None) -> Iterator[str]
def source_path(maildir: Path, source_file: str) -> Path   # the real file for a source_file
def mailbox_for(source_file: str) -> str                   # "maildir/lay-k/inbox/45" -> "lay-k"
```

`discover_email_files` yields `source_file` strings such as `"maildir/lay-k/inbox/45"`: relative, forward slashes, starting with the maildir folder's own name, files only, hidden files and folders skipped. `mailboxes=None` means all mailboxes. Output order is sorted (plain string order), so runs are deterministic. If any named mailbox folder is missing it raises `FileNotFoundError` naming every missing one. This happens when the function is called, before anything is yielded. A mailbox name that is not a plain folder name (`""`, `".."`, `"a/b"`) raises `ValueError`.

*Change from the first version of this plan (D-018): it yielded `Path` and had `source_file_for(path, maildir)`. It now yields the `source_file` string directly, so `source_file_for` is gone and `source_path` is the one place that knows how a `source_file` maps back to a file (relative to the maildir's parent).*

Must NOT: open or read file contents, parse, touch the DB, read config.

### db.py
**Responsibility:** the SQLite write side of ingest, plus the notification update.

```python
def connect(path: str | Path = ":memory:") -> sqlite3.Connection     # PRAGMA foreign_keys = ON; isolation_level=None
def init_schema(conn: sqlite3.Connection, schema_path: Path | None = None) -> None   # drops every table, rebuilds
def insert_email(conn: sqlite3.Connection, record: EmailRecord) -> int                # returns emails.id
def insert_parse_failure(conn: sqlite3.Connection, failure: ParseFailure) -> None
def mark_notified(conn: sqlite3.Connection, message_id: str, when: datetime) -> None
```

`init_schema` drops every existing table and rebuilds from `schema.sql`, so each run starts clean (D-018). `schema_path=None` means the repo-root `schema.sql`. It commits any open transaction.

`insert_email` inserts the email and its recipients as one all-or-nothing unit and raises `sqlite3.IntegrityError` on any constraint error (duplicate `message_id` or `source_file`, bad `copy_of`), leaving no partial rows. It converts `datetime` to ISO 8601 UTC text (naive datetimes raise `ValueError`), `bool` to 0/1 and enums to their string value.

Transactions: `connect` sets `isolation_level=None`, so only the SQL written out starts or ends a transaction. `insert_email` runs inside a `SAVEPOINT`. If the caller opened a transaction (`BEGIN`), a failed insert undoes only itself and the caller commits. If not, the insert is its own transaction and is committed on return. `insert_parse_failure` does not commit.

*Change from the first version of this plan (D-018): `insert_email` returned an `InsertStatus` ("inserted", "duplicate_message_id", "duplicate_source_file") instead of raising. It now raises and returns the new row id, and `InsertStatus` is removed. `init_schema` now drops existing tables first. "Functions do not commit" became the savepoint rule above, so an insert is atomic whether or not the caller has a transaction open.*

Must NOT: parse, find duplicates, send, or import dedup/notify.

### ingest.py
**Responsibility:** wire discover -> parse_headers -> parse_body -> EmailRecord -> db, per file, and survive every kind of bad input.

```python
BodyParser = Callable[[str, str | None, str | None], ParsedBody]   # same arguments as parse_body

STATS_FIELDS: tuple[str, ...]        # the 20 fields of spec Section 3

@dataclass
class ExtractionStats:
    total_found: int
    parsed: int                      # stored in emails; total_found = parsed + failed
    failed: int
    failures_by_reason: dict[str, int]
    field_counts: dict[str, int]     # populated count per STATS_FIELDS entry
    def completeness(self) -> dict[str, float]    # percent 0-100 of parsed emails
    def to_dict(self) -> dict                     # the extraction_stats.json shape

def process_file(maildir: Path, source_file: str, body_parser: BodyParser) -> EmailRecord   # raises ParseError
def ingest_mailboxes(
    conn: sqlite3.Connection,
    maildir: Path,
    mailboxes: Sequence[str] | None,
    body_parser: BodyParser,
    error_log: Path | None = None,
) -> ExtractionStats
def write_error_log(failures: Sequence[ParseFailure], path: Path) -> None   # "<source_file> | <reason>"
def format_stats(stats: ExtractionStats) -> str     # console report; main.py prints it
def write_stats(stats: ExtractionStats, path: Path) -> None   # output/extraction_stats.json
```

`ingest_mailboxes` catches `ParseError` and any other `Exception` per file, turns each into a `ParseFailure`, writes it to the DB and the error log, and keeps going. The pipeline must never crash on bad input. A `ParseError` logs its `reason` as is. Any unexpected exception logs the exception type and message, as `"unexpected <ExceptionType>: <message>"` (whitespace collapsed, so one failure is one log line), so it can be told apart from a known parse failure and grouped in stats. The same rule covers read errors and DB errors on a single file, such as a duplicate Message-ID. A missing mailbox is the one exception: it is a setup mistake, so `FileNotFoundError` propagates before any file is touched. The run is one transaction, committed at the end unless the caller had already opened one.

The body parser is a required argument. `main.py` passes `parse_body.parse_body`, and `ingest` never imports `parse_body`, so it can be tested with a fake. It is called positionally as `body_parser(raw_body, content_type, content_transfer_encoding)`. The object it returns needs the `ParsedBody` attributes. `content_hash` is not set yet (open question 4).

Completeness is a percent of parsed emails that have a populated value: non-null and non-empty, where whitespace-only text and empty lists count as empty. `has_attachment` is a boolean and never null, so it always counts. Recipient lists are reported as `to_addresses`, `cc_addresses` and `bcc_addresses`, as in the spec.

*Change from the first version of this plan (D-018): `inserted` and `skipped_existing` are removed, because the database is rebuilt on every run and a duplicate is now a logged failure. `failures_by_reason`, `to_dict`, `format_stats` and `write_stats` are added. `process_file` takes the `source_file` string and a `body_parser`, and `ingest_mailboxes` takes the `body_parser`. "Re-running is safe because `source_file` is UNIQUE" is gone: a re-run starts from an empty database.*

Must NOT: do header or body parsing itself, run dedup or notify, read config (settings arrive as arguments), let one file stop the run.

### dedup.py
**Responsibility:** find storage copies and fuzzy duplicates, flag them in the DB, and produce the report.

```python
def normalize_subject(subject: str) -> str                           # strips Re:/Fwd:/Fw: prefixes
def default_similarity(a: str, b: str) -> float                      # 0-100, rapidfuzz-backed
def mark_storage_copies(conn: sqlite3.Connection) -> int             # sets copy_of via content_hash; returns rows marked
def flag_duplicates(
    conn: sqlite3.Connection,
    threshold: float = 90.0,
    similarity: Callable[[str, str], float] = default_similarity,
) -> list[DuplicateFlag]
def summarize(flags: Sequence[DuplicateFlag]) -> DedupStats           # groups, flagged, avg group size
def write_report(flags: Sequence[DuplicateFlag], path: Path) -> None  # duplicates_report.csv
```

`flag_duplicates` groups on `from_address` plus the normalized subject, compares bodies, treats the earliest as the original, and sets `is_duplicate`, `duplicate_of` and `similarity_score` on every later one. It returns the flags it set. The similarity function is a parameter, so swapping the metric touches nothing else.

Inputs: a DB connection. Dedup runs its own SQL against `schema.sql`.
Must NOT: import db, ingest or notify, send anything, set `notification_*`.

### notify.py
**Responsibility:** render the spec notification and hand it to a sender.

```python
@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    date: datetime
    references: str                  # message_id of the flagged email
    body: str

class Sender(Protocol):
    def send(self, message: OutgoingEmail) -> SendResult: ...

def render_notification(flag: DuplicateFlag, now: datetime) -> OutgoingEmail
def send_notifications(
    flags: Sequence[DuplicateFlag],
    sender: Sender,
    now: datetime | None = None,
) -> list[SendResult]
def write_send_log(results: Sequence[SendResult], path: Path) -> None   # send_log.csv
```

`send_notifications` catches exceptions per message and records them as `status=SendStatus.FAILED`. It never raises. `now` can be injected for tests.

Inputs: `DuplicateFlag` list and a `Sender`. It never gets a DB connection or settings.
Must NOT: touch the DB, import dedup, decide dry-run vs live (the caller passes the sender), hold credentials.

### config.py
**Responsibility:** the pipeline's default settings in one place.

```python
@dataclass(frozen=True)
class Settings:
    maildir: Path = Path("data/maildir")
    db_path: Path = Path("output/enron.db")
    output_dir: Path = Path("output")
    mailboxes: tuple[str, ...] | None = MAILBOXES  # D-013; None would mean every mailbox
    similarity_threshold: float = 90.0
    send_live: bool = False

MAILBOXES = ("ring-a", "ybarbo-p", "presto-k", "baughman-d", "beck-s")   # D-013, Set 1
def default_settings() -> Settings
```

Only `main.py` imports it. It reads values and passes them down as plain arguments. The other modules keep their own defaults so they work in isolation.

Must NOT: import any package module, do I/O, hold secrets.

### main.py (repo root)
**Responsibility:** the CLI. One command runs the whole pipeline.

```
python main.py [--maildir PATH] [--db PATH] [--output-dir PATH]
               [--mailboxes A B ...] [--threshold 90] [--send-live]
```

Runs: init db -> ingest -> dedup -> notify with `DryRunSender`, or the live sender when `--send-live` is set -> write outputs. Wires results between modules, for example `SendResult` -> `db.mark_notified`. Without `--send-live` nothing is ever sent.

Must NOT: contain parsing, SQL, or notification-rendering logic.

## 2. Record mappings

### EmailRecord <-> `emails` (27 columns, 1:1)

`@dataclass(frozen=True, slots=True)`. Required fields come first. Fields marked **DB-managed** have defaults. Parsers never set them. dedup/db set them, and `EmailRecord` carries them so rows read back from the DB fit the same type.

| Field | Python type | `emails` column | Set by |
|---|---|---|---|
| message_id | str | message_id | parse_headers |
| date | datetime (UTC) | date (ISO 8601 UTC text) | parse_headers |
| from_address | str | from_address | parse_headers |
| subject | str | subject | parse_headers |
| body | str | body | parse_body |
| source_file | str | source_file | ingest (via discover) |
| mailbox | str | mailbox | ingest (via discover) |
| date_raw | str \| None | date_raw | parse_headers |
| x_from / x_to / x_cc / x_bcc | str \| None | same names | parse_headers |
| x_folder / x_origin | str \| None | same names | parse_headers |
| content_type | str \| None | content_type | parse_headers |
| has_attachment | bool | has_attachment (0/1) | parse_body |
| forwarded_content | str \| None | forwarded_content | parse_body |
| quoted_content | str \| None | quoted_content | parse_body |
| headings | str \| None | headings | parse_body |
| content_hash | str \| None | content_hash | parse_body |
| id | int \| None | id | **DB-managed** |
| copy_of | str \| None | copy_of | **DB-managed** (dedup) |
| is_duplicate | bool = False | is_duplicate (0/1) | **DB-managed** (dedup) |
| duplicate_of | str \| None | duplicate_of | **DB-managed** (dedup) |
| similarity_score | float \| None | similarity_score | **DB-managed** (dedup) |
| notification_sent | bool = False | notification_sent (0/1) | **DB-managed** (db.mark_notified) |
| notification_date | datetime \| None | notification_date (ISO text) | **DB-managed** (db.mark_notified) |

Plus `recipients: tuple[Recipient, ...]` (default `()`; a list is converted to a tuple on construction), stored in `recipients`:

| `Recipient` field | `recipients` column |
|---|---|
| address | address |
| kind (`RecipientKind`: `"to"`, `"cc"`, `"bcc"`; stored as the plain text value) | kind |
| position (index within that header) | position |

`recipients.id` and `recipients.email_id` are assigned by `db.insert_email`. ingest builds the recipients from `ParsedHeaders.to/cc/bcc`. `db.insert_email` writes `kind.value`, so the column always holds plain text.

### ParseFailure <-> `parse_failures`

| `ParseFailure` field | Column |
|---|---|
| source_file: str | source_file |
| reason: str | reason |
| failed_at: datetime (UTC) | failed_at (ISO text) |
| (none) | id (DB-assigned) |

ingest sets `failed_at`, since parsers are pure and have no clock.

## 3. Sender interface (notify)

```python
class Sender(Protocol):
    def send(self, message: OutgoingEmail) -> SendResult: ...
```

`SendResult` fields: `timestamp`, `recipient`, `subject`, `status`, `error`, `duplicate_message_id`. The first five are the `send_log.csv` columns. `status` is a `SendStatus`: `SENT`, `DRY_RUN` or `FAILED`.

| Implementation | Behavior |
|---|---|
| `DryRunSender(output_dir: Path)` | Writes one `.eml` per message into `output_dir` (`output/replies/`). Returns `status=SendStatus.DRY_RUN`. Sends nothing. |
| `McpSender(...)` | Sends via the Gmail MCP server's `send_email` tool. Returns `SendStatus.SENT` or `SendStatus.FAILED` with the error text. **Skeleton only.** How it reaches MCP is decided at the notifier (D-014 item 6). |

Both implementations live in `notify.py` for now. If the live sender grows, splitting it into its own module would be a structural change, so I would ask first.

## 4. Data flow

```
data/maildir/<mailbox>/<folder>/<file>
        |
   discover.discover_email_files        -> source_file strings (mailbox via mailbox_for)
        |
   ingest reads the file bytes
        |
   parse_headers.parse_headers(bytes)   -> ParsedHeaders      (ParseError -> ParseFailure)
        |
   parse_body.parse_body(raw_body, content_type, content_transfer_encoding) -> ParsedBody
        |
   ingest assembles                     -> EmailRecord (+ Recipients)
        |
   db.insert_email                      -> emails, recipients   (failures -> parse_failures, error_log.txt)
        |
   dedup.mark_storage_copies / flag_duplicates   (reads and updates emails via the connection)
        |                                -> list[DuplicateFlag]
   notify.send_notifications(flags, sender) -> list[SendResult]
        |
   db.mark_notified (live SendStatus.SENT results only)
```

`main.py` drives this sequence. `ingest.py` owns the file-to-db portion.

## 5. Spec deliverables by module

| Deliverable | Produced by |
|---|---|
| `error_log.txt` | `ingest.write_error_log` (plus the `parse_failures` table via `db.insert_parse_failure`) |
| Extraction stats (found / parsed / failed / completeness) | `ingest.ExtractionStats`, printed by `main.py` |
| `duplicates_report.csv` | `dedup.write_report` |
| Duplicate stats (groups, flagged, average group size) | `dedup.summarize`, printed by `main.py` |
| `output/replies/*.eml` | `notify.DryRunSender` |
| `output/send_log.csv` | `notify.write_send_log` (live sends) |
| `schema.sql` | exists already, loaded by `db.init_schema` |

Not module output, handled separately later: `sample_queries.sql`, `mcp_config.json.example`, README, AI_USAGE.md, requirements.txt.

## 6. Import rules

| Module | May import from the package |
|---|---|
| models | nothing |
| config | nothing |
| parse_headers | models |
| parse_body | models |
| discover | nothing |
| db | models |
| dedup | models |
| notify | models |
| ingest | models, discover, parse_headers, parse_body, db |
| main.py | everything |

- Nothing imports from `scripts/`.
- No module other than `ingest` and `main` imports a sibling (besides `models`).
- `ingest` does not import `dedup` or `notify`. `main` runs those stages.
- Third-party libraries stay inside the one module that needs them (the fuzzy library in `dedup` only).
- `dedup` and `db` both run SQL against `schema.sql`. That is a deliberate, schema-level coupling, not an import.

## 7. Open points to confirm

1. **Extra shared types.** You named `EmailRecord` and `ParseFailure`. I added `Recipient`, `ParseError`, `DuplicateFlag` and `SendResult` to `models.py`, because two modules each need them. `ParsedHeaders`, `ParsedBody`, `OutgoingEmail` and the stats types stay in the module that produces them.
2. **Parsers raise `ParseError` for bad input** instead of returning a result object. ingest converts it to a `ParseFailure` and adds the timestamp.
3. **`EmailRecord` carries the DB-managed columns** (with defaults) so it is literally 1:1 with `emails`.
4. **`content_hash` input is not defined in D-012.** The skeleton only stubs `compute_content_hash`. The definition (body only vs body plus sender and subject) and how `copy_of` interacts with fuzzy `is_duplicate` belong to the dedup plan.
5. **Who gets a notification.** The spec says one email per duplicate group to the sender of the latest duplicate, but also wants drafts for all flagged duplicates. The signatures above work either way, because `send_notifications` sends whatever flags it is given. Which flags `main.py` passes is for you to decide before the notifier is built.
6. **Similarity scale.** I used 0-100 with a default threshold of 90.0, to match the spec's percentage wording and the `schema.sql` REAL column.
