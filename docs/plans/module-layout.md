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
| `DuplicateFlag` | One flagged duplicate: a `duplicates_report.csv` row plus a required `original_subject` (D-024; not a report column). `subject` is the flagged email's own subject, `original_subject` the original's. Produced by dedup, consumed by notify. |
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
def load_emails(conn: sqlite3.Connection, canonical_only: bool = False) -> list[EmailRecord]   # D-021: for dedup
def replace_storage_copies(conn, content_hashes: Mapping[str, str], copy_of: Mapping[str, str]) -> None
def replace_duplicate_flags(conn, flags: Iterable[DuplicateFlag]) -> None
```

`init_schema` drops every existing table and rebuilds from `schema.sql`, so each run starts clean (D-018). `schema_path=None` means the repo-root `schema.sql`. It commits any open transaction.

`insert_email` inserts the email and its recipients as one all-or-nothing unit and raises `sqlite3.IntegrityError` on any constraint error (duplicate `message_id` or `source_file`, bad `copy_of`), leaving no partial rows. It converts `datetime` to ISO 8601 UTC text (naive datetimes raise `ValueError`), `bool` to 0/1 and enums to their string value.

Transactions: `connect` sets `isolation_level=None`, so only the SQL written out starts or ends a transaction. `insert_email` runs inside a `SAVEPOINT`. If the caller opened a transaction (`BEGIN`), a failed insert undoes only itself and the caller commits. If not, the insert is its own transaction and is committed on return. `insert_parse_failure` does not commit.

*Change from the first version of this plan (D-018): `insert_email` returned an `InsertStatus` ("inserted", "duplicate_message_id", "duplicate_source_file") instead of raising. It now raises and returns the new row id, and `InsertStatus` is removed. `init_schema` now drops existing tables first. "Functions do not commit" became the savepoint rule above, so an insert is atomic whether or not the caller has a transaction open.*

*Added for dedup (D-021 item 13: dedup reads and writes only through db functions).* `load_emails` reads rows back as `EmailRecord` in id order (`canonical_only=True` skips rows with `copy_of` set; `recipients` is not loaded). `replace_storage_copies` and `replace_duplicate_flags` write dedup's two stages. "Replace" means each first clears what an earlier run left, so rerunning dedup, or running it at another threshold, leaves nothing stale. They touch only their own columns (never `notification_*`), raise `ValueError` for a `message_id` that is not in the table instead of silently updating nothing, run inside a savepoint like `insert_email`, and do not commit.

`mark_notified` (D-022 item 8) sets `notification_sent = 1` and `notification_date` (ISO 8601 UTC text) on one email, and nothing else. An unknown `message_id` or a naive datetime raises `ValueError`. It does not commit.

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
    has_attachment_true: int         # parsed emails whose has_attachment is True
    def completeness(self) -> dict[str, float]    # percent 0-100 of parsed emails
    def has_attachment_true_percent(self) -> float   # percent 0-100 of parsed emails; 0.0 if none parsed
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

Completeness is a percent of parsed emails that have a populated value: non-null and non-empty, where whitespace-only text and empty lists count as empty. `has_attachment` is a boolean and never null, so it always counts and its completeness is always 100%. The informative number is reported separately as `has_attachment_true_percent` (percent of parsed emails where it is True), in the console report and in `extraction_stats.json`. Recipient lists are reported as `to_addresses`, `cc_addresses` and `bcc_addresses`, as in the spec.

*Change from the first version of this plan (D-018): `inserted` and `skipped_existing` are removed, because the database is rebuilt on every run and a duplicate is now a logged failure. `failures_by_reason`, `to_dict`, `format_stats` and `write_stats` are added. `process_file` takes the `source_file` string and a `body_parser`, and `ingest_mailboxes` takes the `body_parser`. "Re-running is safe because `source_file` is UNIQUE" is gone: a re-run starts from an empty database.*

Must NOT: do header or body parsing itself, run dedup or notify, read config (settings arrive as arguments), let one file stop the run.

### dedup.py
**Responsibility:** find storage copies and fuzzy duplicates, flag them in the DB, and produce the report.

```python
def normalize_subject(subject: str) -> str                           # strips Re:/Fwd:/Fw: prefixes
def default_similarity(a: str, b: str) -> float                      # 0-100, rapidfuzz.fuzz.ratio
def content_hash_for(email: EmailRecord) -> str                      # SHA-256 of the stage 1 fields (D-021 item 2)
def mark_storage_copies(conn: sqlite3.Connection) -> StorageCopyStats   # stage 1: fills content_hash, sets copy_of
def flag_duplicates(
    conn: sqlite3.Connection,
    threshold: float = 90.0,
    similarity: Callable[[str, str], float] = default_similarity,
) -> list[DuplicateFlag]
def summarize(flags: Sequence[DuplicateFlag], storage: StorageCopyStats | None = None) -> DedupStats
def write_report(flags: Sequence[DuplicateFlag], path: Path) -> None  # duplicates_report.csv
def write_stats(stats: DedupStats, path: Path) -> None                # output/dedup_stats.json

StorageCopyStats(groups, rows)
DedupStats(storage_copy_groups, storage_copy_rows, duplicate_groups, emails_flagged,
           average_group_size, largest_group_size)
```

`flag_duplicates` groups on `from_address` plus the normalized subject, compares bodies, treats the earliest as the original, and sets `is_duplicate`, `duplicate_of` and `similarity_score` on every later one. It returns the flags it set. The similarity function is a parameter, so swapping the metric touches nothing else.

Behavior is fixed by D-021, with stage 1 extended by D-023: stage 1 collapses storage copies (exact copies by content hash, plus timestamp-skewed copies: same sender, subject as written and main body, date gap a whole number of hours from 1 to 12, merged without chaining), and stage 2 runs the spec rule on canonical rows only, with no chaining.

Inputs: a DB connection and the threshold. Dedup reads and writes through the `db` functions above and runs no SQL of its own.
Must NOT: import ingest or notify, send anything, set `notification_*`.

*Changes from the first version of this plan (D-021): dedup now imports `db` (D-021 item 13; the table in section 6 changes from "models" to "models, db"). `mark_storage_copies` returns `StorageCopyStats(groups, rows)` instead of an int, because the stats need the group count. `DedupStats` gained the storage-copy numbers and `largest_group_size`, and `groups` is now `duplicate_groups`. `content_hash_for`, `StorageCopyStats` and `write_stats` are new. `content_hash` is computed by dedup from the stored columns, not by ingest, so `parse_body.compute_content_hash` is no longer needed.*

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

class DryRunSender:                  # D-022 item 1
    def __init__(self, output_dir: Path) -> None
class McpSender:                     # D-022 item 3, D-024 items 3 to 6
    def __init__(self, notify_to: str | None, *, connect=None, command="npx",
                 args=("-y", "@gongrzhe/server-gmail-autoauth-mcp"), call_timeout_seconds=60.0) -> None
    def send(self, message: OutgoingEmail) -> SendResult      # never raises
    def close(self) -> None                                   # also a context manager

def render_notification(flag: DuplicateFlag, now: datetime) -> OutgoingEmail
def notice_subject(original_subject: str) -> str              # D-024 item 1
def live_body(message: OutgoingEmail) -> str                    # D-024 item 5
def select_live_flags(flags: Sequence[DuplicateFlag], limit: int) -> list[DuplicateFlag]
def send_notifications(
    flags: Sequence[DuplicateFlag],
    sender: Sender,
    now: datetime | None = None,
) -> list[SendResult]
def write_send_log(results: Sequence[SendResult], path: Path, *, append: bool = False) -> None
```

Behavior is fixed by D-022, amended by D-024.

- `render_notification` fills the spec's section 4.2 template. `OutgoingEmail.to` is the sender of the flagged duplicate (the template's To line). The subject is `notice_subject(flag.original_subject)`: `"[Duplicate Notice] Re: "` plus the ORIGINAL email's subject, with no second `Re:` if it already starts with one (any case), newlines removed (D-024; the body's "Subject:" line under "Your Email (Flagged)" stays the flagged email's own), and `references` is the flagged message id. The body has every word of the 13 template lines, in order, laid out as D-026 says: blocks joined by one blank line, the Flagged and Original blocks indented, and their labels padded so the values line up (a golden test pins the exact text). The spec shows a blank line between every line, but that is an artifact of the Word document. The same renderer produces the `.eml` drafts and the live email, which adds the four header lines and a blank line in front (`live_body`). Dates in the body are ISO 8601 UTC (`2001-05-14T23:39:00Z`), and the score has two decimals and a `%`. `DuplicateFlag` carries both subjects: `original_subject` fills the Subject header (D-024) and `subject`, the flagged email's own, fills the body's `Subject:` line.
- `select_live_flags` returns the latest duplicate (by `duplicate_date`, ties by message id) of each group (flags sharing `original_message_id`). Groups come in `duplicates_report.csv` order and the first `limit` are kept. A negative limit raises `ValueError`.
- `send_notifications` catches exceptions while rendering or sending, per message, and records them as `status=SendStatus.FAILED` with `"<ExceptionType>: <message>"`. It never raises. Without `now`, each message is stamped with the current UTC time in whole seconds.
- `write_send_log` writes `timestamp, recipient, subject, status, error`, one row per attempt. By default it rewrites the file; with `append=True` it adds rows after the existing ones and writes the header only for a new or empty file. Live runs append, dry runs rewrite (D-024 item 7).
- `DryRunSender` creates its folder and deletes earlier drafts (only files named `<digits>_*.eml`), then writes `<n>_<sanitized message id>.eml` per message, n from 1, zero-padded to 4 digits. Each draft is a plain-text RFC 822 message with To, Subject, Date and References headers. Its results have status `DRY_RUN`, the Enron sender as `recipient`, and the message's Date as `timestamp`.
- `McpSender(notify_to)` raises `ValueError` mentioning `NOTIFY_TO` if the address is missing or has no `@`: that is how live mode refuses to start. Every live email is delivered to `notify_to`, while the body keeps the Enron sender in its To line. It is a real MCP client, described in the next subsection.

*Added by D-022: `DryRunSender` and `McpSender` were skeletons with no constructor arguments (McpSender) and a stub `send` (DryRunSender). `select_live_flags` is new. `send_notifications` also catches rendering errors, not only send errors.*

#### The live sender (D-024)

`McpSender` is an MCP client using the official `mcp` SDK over stdio. It launches `npx -y @gongrzhe/server-gmail-autoauth-mcp` and calls the server's `send_email` tool. The SDK is async and `Sender.send` is not, so the session lives in an event loop on a background thread (anyio's blocking portal) and `send` waits for each call.

- **One session per run.** It opens on the first send (so a refused or zero-send run starts no process), is reused for every message, and is closed by `close()` or the end of a `with` block. `main` closes it in a `finally`.
- **Arguments come from the server, not from memory.** `scripts/list_mcp_tools.py` connects, calls `list_tools()` and prints each tool's schema (it sends nothing). The `send_email` schema it printed:

| Property | Type | Required | We send |
|---|---|---|---|
| `to` | array of string | yes | `[NOTIFY_TO]` |
| `subject` | string | yes | the notice subject |
| `body` | string | yes | `live_body(message)` |
| `mimeType` | `text/plain`, `text/html` or `multipart/alternative` (default `text/plain`) | no | `"text/plain"` |
| `inReplyTo` | string, "Message ID being replied to" | no | the flagged email's message id |
| `htmlBody`, `cc`, `bcc`, `threadId`, `attachments` | | no | not used |

  The schema sets `additionalProperties: false`, so any other key is rejected, and there is no free-form headers field.
- **References.** There is no way to set a `References` header directly. `inReplyTo` is passed instead, which is the closest thing the tool offers (how the server renders it as a header is not verified; check the first live email). The spec's `References: <message id>` line is also kept in the body: `live_body` puts the template's four header lines (To, Subject, Date, References) at the top, then a blank line, then the notification text.
- **Failures never raise into the pipeline.** A tool error (`is_error`), an `MCPError` or a 60-second timeout fails that one message and the session carries on. Any other failure, including a server that cannot start, loses the session: that message and all later ones fail with the reason, with no reconnect and no retry, so a broken setup cannot start a subprocess per email.
- **Result.** `SENT` or `FAILED`, `recipient = notify_to`, `timestamp` = the message's Date, `error` = a short reason (`"send_email failed: ..."`, `"MCP error: ..."`, `"timed out after 60s ..."`, `"MCP session lost: <Type>: <message>"`, `"could not start MCP server: ..."`).

Inputs: `DuplicateFlag` list and a `Sender`. It never gets a DB connection or settings.
Must NOT: touch the DB, import dedup, decide dry-run vs live (the caller passes the sender), hold credentials.

### config.py
**Responsibility:** the pipeline's default settings in one place.

```python
# Named constants (D-025): every default is defined here and nowhere else.
MAILBOXES = ("ring-a", "ybarbo-p", "presto-k", "baughman-d", "beck-s")   # D-013, Set 1
MAILDIR, OUTPUT_DIR = Path("data/maildir"), Path("output")
DB_FILE, EXTRACTION_STATS_FILE, DEDUP_STATS_FILE = "enron.db", "extraction_stats.json", "dedup_stats.json"
REPLIES_DIR, SEND_LOG_FILE, DRY_RUN_LOG_FILE = "replies", "send_log.csv", "send_log_dry_run.csv"
ERROR_LOG, DUPLICATES_REPORT = Path("error_log.txt"), Path("duplicates_report.csv")   # repo root: deliverables
NOTIFY_TO_ENV = "NOTIFY_TO"
SIMILARITY_THRESHOLD, LIVE_SEND_LIMIT = 90.0, 10     # the spec value; D-022 item 4

@dataclass(frozen=True)
class Settings:
    maildir: Path = MAILDIR
    db_path: Path = OUTPUT_DIR / DB_FILE
    output_dir: Path = OUTPUT_DIR
    error_log: Path = ERROR_LOG
    duplicates_report: Path = DUPLICATES_REPORT
    mailboxes: tuple[str, ...] | None = MAILBOXES  # None would mean every mailbox
    similarity_threshold: float = SIMILARITY_THRESHOLD
    send_live: bool = False
    limit: int = LIVE_SEND_LIMIT
    notify_to: str | None = None                   # filled by main from --notify-to or NOTIFY_TO

    # derived from output_dir, so no other file knows the names:
    extraction_stats, dedup_stats, replies_dir     # properties
    send_log                                       # property: send_log.csv if send_live else send_log_dry_run.csv

def default_settings() -> Settings
```

*Changed by D-025: the literals that used to sit in `main.py` (output file names, the database name) and the recipient moved here. `notify_to` is the one field that carries something private-ish, and it is only an address the user passes in; the default is `None`.*

Only `main.py` imports it. It reads values, lets command-line options override them, and passes the result down as plain arguments; no other module reads argv or the environment (`tests/unit/test_no_hidden_settings.py` checks this on the parsed code). The other modules keep their own defaults so they work in isolation, and a test keeps dedup's threshold default equal to `SIMILARITY_THRESHOLD`.

Must NOT: import any package module, do I/O, hold secrets.

### main.py (repo root)
**Responsibility:** the CLI. One command runs the whole pipeline.

```
python main.py [--maildir PATH] [--mailboxes NAME [NAME ...]] [--output-dir PATH]
               [--db-path PATH] [--threshold 0-100] [--send-live] [--limit N]
               [--notify-to ADDRESS]
```

Defaults come from `config.py` and are shown in `--help`. `--db` is kept as an older name for `--db-path`. `--notify-to` wins over the `NOTIFY_TO` environment variable; live mode needs one of the two. Checks run before anything is created, wiped or written (D-025): a threshold outside 0 to 100 or a mailbox name that is not a plain folder name is a usage error (exit 2); a missing maildir, a missing mailbox folder or a missing live recipient prints one `error:` line and exits 1. Repeated mailbox names are dropped.

```python
def settings_from_args(argv: Sequence[str] | None = None) -> Settings
def check_inputs(settings: Settings) -> None   # D-025: maildir and every mailbox must exist; raises FileNotFoundError
def run_notifications(conn, flags, sender, *, live: bool, limit: int, send_log: Path) -> list[SendResult]
def run_pipeline(settings: Settings, body_parser=parse_body, sender: Sender | None = None) -> ExtractionStats
def main(argv: Sequence[str] | None = None) -> int
```

- `settings_from_args` starts from `default_settings()` and applies the flags. `--output-dir` also moves the database to `<output-dir>/enron.db` unless `--db` is given. The error log and `duplicates_report.csv` stay at the repo root (`Settings.error_log`, `Settings.duplicates_report`) whatever `--output-dir` says. `--mailboxes` replaces the D-013 list. `--limit N` (default 10, must not be negative) caps live sends only.
- `run_notifications` is the notify stage, kept in `main.py` because it ties `notify` to the database and `notify` never touches the DB. Dry run: every flag gets a draft. Live: `notify.select_live_flags(flags, limit)` first. Either way it calls `notify.send_notifications`, writes the log it is given (a live run appends, a dry run rewrites), and calls `db.mark_notified` for each result with status `SENT` (D-022 items 8 and 9, D-024 item 7). Dry runs and failures never mark anything.
- `run_pipeline` is the pipeline sequence, kept apart from argument parsing so tests can call it with a `Settings`, a fake `body_parser` and a fake `sender`. It first calls `check_inputs`, so a missing maildir or mailbox raises before anything is touched (the previous database survives a typo); then it creates the output folder and rebuilds the database (`db.init_schema`). Then ingest (writes `extraction_stats.json`, prints `format_stats`), dedup (`mark_storage_copies`, `flag_duplicates` with `settings.similarity_threshold`, `summarize`, `write_report`, `output/dedup_stats.json`, prints the stats), and notify (`DryRunSender(output/replies)` unless live, then `run_notifications`, then a one-line summary). The log is `output/send_log.csv` in live mode and `output/send_log_dry_run.csv` in a dry run (`send_log_path`; D-024 item 7). It raises `FileNotFoundError` for a missing maildir or mailbox, and `ValueError` if `send_live` is set with no `sender`.
- `settings_from_args` resolves the live recipient: `--notify-to`, else the `NOTIFY_TO` environment variable (read there and nowhere in the package). `main` builds `notify.McpSender` from `settings.notify_to` in live mode only, **before any work starts**, so a missing or unusable address stops the run with `error: NOTIFY_TO ... Give the recipient with --notify-to ADDRESS or the NOTIFY_TO environment variable.` on stderr and exit code 1, with nothing ingested or written. It closes the sender in a `finally` when the run ends, however it ends. It also turns `FileNotFoundError` into a one-line `error: ...` and exit code 1. Success returns 0, including a live run in which some sends failed (they are in `send_log.csv` and the console summary).

Without `--send-live` nothing is ever sent. `send_log.csv` is the log of live sends only (it is the spec's deliverable of that name); dry-run attempts go to `send_log_dry_run.csv`.

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
| `McpSender(notify_to: str)` | Sends via the Gmail MCP server's `send_email` tool to `notify_to` (the `NOTIFY_TO` address; D-022 item 3), while the message keeps the Enron sender in its To line. Returns `SendStatus.SENT` with `recipient=notify_to`, or `SendStatus.FAILED` with the error text, and never raises. One MCP session for the whole run (D-024). |

Both implementations live in `notify.py` for now. `McpSender` is now a few hundred lines with the MCP libraries (`mcp`, `anyio`) imported at the top of `notify.py`; splitting the live sender into its own module would be a structural change, so it is left for you to decide.

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
   main.run_notifications: notify.select_live_flags (live only) -> notify.send_notifications(flags, sender) -> list[SendResult]
        |                                -> output/send_log.csv (live, appended) or output/send_log_dry_run.csv (dry run)
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
| `output/send_log.csv` | `notify.write_send_log(append=True)` (live attempts only; appended, never rewritten) |
| `output/send_log_dry_run.csv` | `notify.write_send_log` (dry-run attempts; rewritten each dry run) |
| `mcp_config.json.example` | repo root, checked by `tests/unit/test_mcp_config_example.py` |
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
| dedup | models, db |
| notify | models |
| ingest | models, discover, parse_headers, parse_body, db |
| main.py | everything |

- Nothing imports from `scripts/`.
- No module other than `ingest` and `main` imports a sibling (besides `models`).
- `ingest` does not import `dedup` or `notify`. `main` runs those stages.
- Third-party libraries stay inside the one module that needs them (the fuzzy library in `dedup` only).
- `dedup` calls `db` functions for every read and write (D-021 item 13), so it does import `db`. It still does not run its own SQL.

## 7. Open points to confirm

1. **Extra shared types.** You named `EmailRecord` and `ParseFailure`. I added `Recipient`, `ParseError`, `DuplicateFlag` and `SendResult` to `models.py`, because two modules each need them. `ParsedHeaders`, `ParsedBody`, `OutgoingEmail` and the stats types stay in the module that produces them.
2. **Parsers raise `ParseError` for bad input** instead of returning a result object. ingest converts it to a `ParseFailure` and adds the timestamp.
3. **`EmailRecord` carries the DB-managed columns** (with defaults) so it is literally 1:1 with `emails`.
4. **`content_hash` input** was not defined in D-012. Resolved by D-021: SHA-256 of from_address, date (UTC ISO), subject as written, body, quoted_content and forwarded_content, computed by dedup (`content_hash_for`). A flagged row's storage copies are not flagged; they stay reachable through `copy_of`.
5. **Who gets a notification.** The spec says one email per duplicate group to the sender of the latest duplicate, but also wants drafts for all flagged duplicates. The signatures above work either way, because `send_notifications` sends whatever flags it is given. Which flags `main.py` passes is for you to decide before the notifier is built. **Resolved by D-022:** a dry run passes every flag (one draft each); a live run passes only the latest duplicate of each group, capped by `--limit`.
6. **Similarity scale.** I used 0-100 with a default threshold of 90.0, to match the spec's percentage wording and the `schema.sql` REAL column.
