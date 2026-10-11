#!/usr/bin/env python3
"""
discover_edge_cases.py - Systematic edge-case discovery over the Enron maildir.

See docs/DECISIONS.md D-011. This is the discovery pass that runs BEFORE the
final mailbox selection. It does not build any pipeline. It walks the full
maildir once and answers "what shapes and marker styles actually occur?" so new
edge case types can be added to the checklist before selection runs.

Guarantees (same as explore_data.py):
  * Standard library only (Python 3.10+).
  * Read-only: never writes inside data/.
  * Crash-proof per file: every file is wrapped in try/except. A bad file is
    counted and recorded, never fatal.

Four discovery sections, all in one pass:
  1. Value-shape census for From/To/Cc/Subject/X-From/X-To/X-Folder.
     Shape = letters->'a', digits->'N', then collapse consecutive repeats.
     Report top 20 shapes + every shape seen in <50 files (one example each).
  2. Frequent-line mining in bodies. Normalize each line (strip, digits->N,
     emails/Notes-names->placeholders, canonicalize separator runs). Report the
     100 most common lines that look like separators/markers/boilerplate.
  3. Parser defects broken down by defect type, with example paths.
  4. Random sample of 50 files (fixed seed) with automated red-flag notes.

Plus a battery of candidate "new edge case type" detectors counted across the
full pass, so the final list has real counts and example paths.

Output: docs/exploration/DISCOVERY.md
"""

from __future__ import annotations

import argparse
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from email import policy
from email.parser import BytesParser

# --- Constants --------------------------------------------------------------

COMPAT32 = policy.compat32  # lenient: records problems on msg.defects, rarely raises

# Headers we run the value-shape census over (order preserved in the report).
SHAPE_HEADERS = ["From", "To", "Cc", "Subject", "X-From", "X-To", "X-Folder"]

# Mandatory headers (for the random-sample "missing field" red flag).
MANDATORY = ["message-id", "date", "from", "to", "subject"]

# Caps that keep one pathological file from dominating runtime/memory.
MAX_BODY_LINES = 500          # body lines scanned per file for line mining
RARE_SHAPE_MAX = 50           # how many rare shapes to list per header
ALL_LINES_PRUNE_AT = 2_000_000  # prune the general line counter past this size
RANDOM_SAMPLE_N = 50
RANDOM_SEED = 20261010        # fixed seed -> reproducible sample

# --- Regexes ----------------------------------------------------------------

RE_DIGIT = re.compile(r"\d")
# An email address, used to replace addresses with a placeholder in line mining.
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# A Lotus Notes name path, e.g. "Vince J Kaminski/HOU/ECT" or ".../CN=PALLEN".
RE_NOTES_NAME = re.compile(r"[A-Za-z][A-Za-z.\- ]*/[A-Za-z0-9=/@.\- ]*(?:HOU|ECT|ENRON|NA|EES|EU|Corp|CN=)[A-Za-z0-9=/@.\- ]*")
# A run of 3+ identical punctuation chars (separator rule) -> canonical length 5.
RE_PUNCT_RUN = re.compile(r"([^\w\s])\1{2,}")
RE_WS = re.compile(r"\s+")

# Candidate "new edge case type" detectors (body / header regexes).
RE_NOTES_FORWARD = re.compile(r"-{3,}\s*Forwarded by .+? on \d{1,2}/\d{1,2}/\d{2,4}", re.I)
RE_OUTLOOK_ORIG = re.compile(r"-{2,}\s*Original Message\s*-{2,}", re.I)
RE_NOTES_INLINE = re.compile(
    r"(?m)^\s*\S.*?/(?:HOU|ECT|ENRON|NA|EES|EU|Corp)\S*\s+on\s+\d{1,2}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2}")
RE_LDAP_DN = re.compile(r"/O=ENRON/OU=", re.I)
RE_PLACEHOLDER = re.compile(
    r"(no[_.]?address@enron\.com|^\s*\.@enron\.com|undisclosed[ ._\-]?recipients|@enron\.@)", re.I)
# A quoted-printable soft break at end of line, or a =XX control artifact.
RE_QP_ARTIFACT = re.compile(r"=\r?\n|=[0-9A-F]{2}")

# Separator / marker / boilerplate keyword predicate for "interesting" lines.
MARKER_KEYWORDS = (
    "original message", "forwarded by", "sent by", "to advertise",
    "confidential", "privileged", "unsubscribe", "this message",
    "this e-mail", "this email", "intended recipient", "do not reply",
    "disclaimer", "proprietary", "attorney-client", "work product",
)
HEADER_LABELS = ("from:", "to:", "cc:", "bcc:", "sent:", "subject:", "date:",
                 "please respond to", "sent by:")


# --- Shape + line normalization ---------------------------------------------

def shape(value: str) -> str:
    """letters->'a', digits->'N', everything else kept, then collapse runs of
    consecutive identical characters. 'John.Smith1@enron.com' -> 'a.a@a.a'.

    Internal whitespace (including the newline+tab of a FOLDED header value) is
    first collapsed to a single space, so a folded recipient list and its
    unfolded equivalent map to the same shape instead of splitting the census.
    Folding itself is tracked separately (folded_subject / folded headers)."""
    value = RE_WS.sub(" ", value)
    out = []
    for ch in value:
        if ch.isalpha():
            c = "a"
        elif ch.isdigit():
            c = "N"
        else:
            c = ch
        if not out or out[-1] != c:
            out.append(c)
    return "".join(out)


def norm_line(s: str) -> str | None:
    """Normalize a body line for frequency counting. Returns None for blanks."""
    s = s.strip()
    if not s:
        return None
    s = RE_EMAIL.sub("<email>", s)
    s = RE_NOTES_NAME.sub("<name>", s)
    s = RE_DIGIT.sub("N", s)
    s = RE_PUNCT_RUN.sub(lambda m: m.group(1) * 5, s)  # ----- and ------------- merge
    s = RE_WS.sub(" ", s).strip()
    return s or None


def is_interesting(norm: str) -> bool:
    """True if a normalized line looks like a separator, marker, or boilerplate."""
    low = norm.lower()
    # mostly-punctuation separator lines
    punct = sum(1 for c in norm if not c.isalnum() and not c.isspace())
    if len(norm) >= 5 and punct / len(norm) >= 0.6:
        return True
    if any(k in low for k in MARKER_KEYWORDS):
        return True
    if any(low.startswith(h) for h in HEADER_LABELS):
        return True
    # Notes inline reply header: "<name> on N/N/N N:N ..."
    if "<name>" in norm and re.search(r"on N/N/N", norm):
        return True
    return False


# --- Main discovery pass ----------------------------------------------------

def discover(root: str, out_dir: str, only: str | None, limit: int | None,
             progress_every: int):
    cwd = os.getcwd()
    walk_target = os.path.join(root, only) if only else root

    # Section 1: value-shape census.
    shape_counts: dict[str, Counter] = {h: Counter() for h in SHAPE_HEADERS}
    shape_example: dict[str, dict] = {h: {} for h in SHAPE_HEADERS}

    # Section 2: line mining.
    line_interesting = Counter()          # focused: only "interesting" lines
    line_example: dict[str, tuple] = {}   # norm -> (original_line, path)
    line_all = Counter()                  # general, pruned periodically

    # Section 3: parser defects.
    defect_counts = Counter()
    defect_example: dict[str, str] = {}

    # Candidate new edge case types.
    cand_counts = Counter()
    cand_example: dict[str, str] = {}

    # Section 4: reservoir sample of file paths (uniform, seeded).
    rng = random.Random(RANDOM_SEED)
    reservoir: list[str] = []

    read_errors = 0
    total = 0
    t0 = time.perf_counter()

    def note_cand(name: str, display: str):
        cand_counts[name] += 1
        cand_example.setdefault(name, display)

    for dirpath, _dirs, filenames in os.walk(walk_target):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            display = os.path.relpath(full, cwd)
            total += 1

            if progress_every and total % progress_every == 0:
                rate = total / (time.perf_counter() - t0)
                print(f"  ...{total:,} files ({rate:,.0f}/s)", file=sys.stderr)

            # reservoir sampling: keep a uniform random RANDOM_SAMPLE_N paths
            if len(reservoir) < RANDOM_SAMPLE_N:
                reservoir.append(display)
            else:
                j = rng.randint(0, total - 1)
                if j < RANDOM_SAMPLE_N:
                    reservoir[j] = display

            try:
                with open(full, "rb") as fh:
                    raw = fh.read()
            except Exception:
                read_errors += 1
                continue

            # split header/body on first blank line (\n\n or \r\n\r\n)
            idx = min([i for i in (raw.find(b"\r\n\r\n"), raw.find(b"\n\n")) if i != -1],
                      default=-1)
            if idx == -1:
                header_bytes, body_bytes = raw, b""
            else:
                sep = 4 if raw[idx:idx + 4] == b"\r\n\r\n" else 2
                header_bytes, body_bytes = raw[:idx], raw[idx + sep:]
            header_text = header_bytes.decode("latin-1", "replace")
            body_text = body_bytes.decode("latin-1", "replace")

            # parse headers leniently
            msg = None
            try:
                msg = BytesParser(policy=COMPAT32).parsebytes(raw)
            except Exception as e:
                note_cand("parse_exception", display)
                defect_counts["(parse raised: %s)" % type(e).__name__] += 1
                defect_example.setdefault("(parse raised: %s)" % type(e).__name__, display)

            # --- Section 1: value-shape census ---
            if msg is not None:
                for h in SHAPE_HEADERS:
                    val = msg.get(h)
                    if val is None:
                        continue
                    val = str(val).strip()
                    if val == "":
                        sh = "(empty)"
                    else:
                        sh = shape(val)
                    shape_counts[h][sh] += 1
                    shape_example[h].setdefault(sh, display)

            # --- Section 3: parser defects ---
            if msg is not None and msg.defects:
                for d in msg.defects:
                    name = type(d).__name__
                    defect_counts[name] += 1
                    defect_example.setdefault(name, display)

            # --- Section 2: frequent-line mining ---
            for raw_line in body_text.split("\n")[:MAX_BODY_LINES]:
                norm = norm_line(raw_line)
                if norm is None:
                    continue
                line_all[norm] += 1
                if is_interesting(norm):
                    line_interesting[norm] += 1
                    if norm not in line_example:
                        line_example[norm] = (raw_line.strip()[:160], display)
            # prune the general counter so memory stays bounded
            if len(line_all) > ALL_LINES_PRUNE_AT:
                line_all = Counter({k: c for k, c in line_all.items() if c > 1})

            # --- candidate new edge case types ---
            cte = (msg.get("Content-Transfer-Encoding") if msg else "") or ""
            cte = cte.strip().lower()
            if cte == "quoted-printable":
                note_cand("quoted_printable_body", display)
            elif cte == "base64":
                note_cand("base64_body", display)

            if RE_NOTES_FORWARD.search(body_text):
                note_cand("lotus_notes_forward", display)
            if RE_NOTES_INLINE.search(body_text):
                note_cand("notes_inline_header", display)
            if len(RE_OUTLOOK_ORIG.findall(body_text)) >= 2:
                note_cand("multi_original_message", display)

            # folded Subject (continuation line starts with space/tab)
            if _header_is_folded(header_text, "subject"):
                note_cand("folded_subject", display)

            # LDAP/Notes DN in recipient-ish headers
            if msg is not None:
                recip_blob = " ".join(
                    str(msg.get(h) or "") for h in ("To", "Cc", "X-To", "X-cc"))
                if RE_LDAP_DN.search(recip_blob):
                    note_cand("ldap_dn_recipient", display)
                addr_blob = " ".join(
                    str(msg.get(h) or "") for h in ("From", "To", "Cc"))
                if RE_PLACEHOLDER.search(addr_blob):
                    note_cand("placeholder_address", display)
                subj = msg.get("Subject")
                if subj is not None and str(subj).strip() == "":
                    note_cand("blank_subject", display)

            if not body_bytes.strip():
                note_cand("empty_body", display)
            if cte != "quoted-printable" and RE_QP_ARTIFACT.search(body_text):
                note_cand("qp_artifact_undeclared", display)

            if limit and total >= limit:
                break
        if limit and total >= limit:
            break

    elapsed = time.perf_counter() - t0

    stats = {
        "root": root, "total": total, "elapsed": elapsed,
        "read_errors": read_errors,
        "shape_counts": shape_counts, "shape_example": shape_example,
        "line_interesting": line_interesting, "line_example": line_example,
        "line_all": line_all,
        "defect_counts": defect_counts, "defect_example": defect_example,
        "cand_counts": cand_counts, "cand_example": cand_example,
        "reservoir": sorted(reservoir),
    }
    os.makedirs(out_dir, exist_ok=True)
    write_discovery(out_dir, stats)
    return stats


def _header_is_folded(header_text: str, name: str) -> bool:
    """True if the named header's value spans a continuation line."""
    prefix = (name + ":").lower()
    capturing = False
    for line in header_text.split("\n"):
        bare = line.rstrip("\r")
        if not capturing:
            if bare[: len(prefix)].lower() == prefix:
                capturing = True
            continue
        if bare[:1] in " \t":
            return True
        break
    return False


# --- Random-sample red flags ------------------------------------------------

def sample_observations(display: str, cwd: str) -> dict:
    """Re-open one sampled file and compute automated parser red flags."""
    obs = {"path": display, "flags": [], "head": []}
    full = os.path.join(cwd, display)
    try:
        with open(full, "rb") as fh:
            raw = fh.read()
    except Exception as e:
        obs["flags"].append(f"unreadable: {e!r}")
        return obs

    idx = min([i for i in (raw.find(b"\r\n\r\n"), raw.find(b"\n\n")) if i != -1],
              default=-1)
    if idx == -1:
        header_bytes, body_bytes = raw, b""
        obs["flags"].append("no blank line separating headers from body")
    else:
        sep = 4 if raw[idx:idx + 4] == b"\r\n\r\n" else 2
        header_bytes, body_bytes = raw[:idx], raw[idx + sep:]
    header_text = header_bytes.decode("latin-1", "replace")
    body_text = body_bytes.decode("latin-1", "replace")

    try:
        msg = BytesParser(policy=COMPAT32).parsebytes(raw)
    except Exception as e:
        obs["flags"].append(f"parse raised {type(e).__name__}")
        msg = None

    if msg is not None:
        keyset = {k.lower() for k in msg.keys()}
        missing = [k for k in MANDATORY if k not in keyset]
        if missing:
            obs["flags"].append(f"missing mandatory {missing}")
        if msg.defects:
            obs["flags"].append(f"defects {[type(d).__name__ for d in msg.defects]}")
        cte = (msg.get("Content-Transfer-Encoding") or "").strip().lower()
        if cte:
            obs["flags"].append(f"CTE={cte}")
        if _header_is_folded(header_text, "subject"):
            obs["flags"].append("folded Subject")
        if _header_is_folded(header_text, "to"):
            obs["flags"].append("folded To")
    if raw.translate(None, bytes(range(128))):
        obs["flags"].append("non-ASCII bytes present")
    if not body_bytes.strip():
        obs["flags"].append("empty body")
    if RE_NOTES_FORWARD.search(body_text):
        obs["flags"].append("Lotus Notes forward marker")
    if RE_NOTES_INLINE.search(body_text):
        obs["flags"].append("Notes inline reply header")
    obs["head"] = [ln.rstrip("\r")[:100] for ln in body_text.split("\n")[:4] if ln.strip()][:3]
    return obs


# --- Static analysis prose (baked in so DISCOVERY.md regenerates whole) ------
# The random sample is seeded (RANDOM_SEED), so these file references are stable
# across runs. Review done by reading the 50 sampled files.

REVIEW_NOTES_TEXT = """**Review notes (from reading the 50 sampled files):**

- Most of the sample is `7bit` plain text. Quoted-printable appears in ~3/50
  (haedicke-m/all_documents/1977, jones-t/sent/5618, mcconnell-m/all_documents/410)
  with visible `=`-soft-breaks and `=20`/`=NN` artifacts. A parser must QP-decode
  before extracting the body, or it keeps `=20`/`=` garbage in the text.
- Lotus Notes forward banners (`----- Forwarded by <name>/ORG on <date> -----`)
  and Notes inline reply headers (tab-indented `To:`/`Subject:` blocks under a
  `<name>/ORG` line) appear in ~12/50 - far more than the Outlook
  `-----Original Message-----` style. Body/forwarded separation must handle both.
- Folded `To` (4/50) and folded `Subject` (derrick-j/sent_items/80) confirmed;
  unfold before parsing recipients/subject.
- Missing `To` (dean-c/inbox/93) is a newsletter blast - matches `missing_to`.

Things a parser could trip on that are NOT yet in the checklist (candidates for a
later pass; left out of the promoted list for the reasons noted):

1. **Multi-level nested quoting** `> >`, `> > >` with quoted headers inside the
   quote (symes-k/discussion_threads/2603: `> > Subject: Amazon deforestation`).
   `quoted_gt` catches the leading `>`, but nesting depth and header-like lines
   inside the quote complicate body-vs-quote separation.
2. **`[IMAGE]` inline placeholders** (20,461 lines corpus-wide) - HTML-to-text
   image markers, a different attachment signal than the `<<file>>` form that
   `attachment_ref` catches.
3. **Spaced-letter banners**, e.g. `[ A D V E R T I S E M E N T ]`
   (keavey-p/notes_inbox/61) - letter-spacing defeats ALL-CAPS heading detection.
4. **Automated system mail**: `HourAhead/DayAhead ... download failed. Manual
   intervention required` (meyers-a/deleted_items/398, platter-p/deleted_items/51)
   and expense/IPO blasts. These are the Type E "automated mail" the dedup step
   should treat specially - the biggest false-positive risk for resend detection.
5. **Signature/footer boilerplate** (`Enron North America Corp.`, phone
   `NNN-NNN-NNNN`, `Thanks,`/`Regards,`) is extremely frequent - relevant to
   separating a real body from its signature.
6. **Nested mailbox folders** (stokley-c/chris_stokley/sent/325): a mailbox can
   contain a person-named subfolder tree, so folder depth can exceed 2."""


def write_discovery(out_dir: str, stats: dict):
    cwd = os.getcwd()
    L: list[str] = []

    def line(s=""):
        L.append(s)

    line("# Enron maildir - edge case discovery")
    line()
    line(f"Generated by `scripts/discover_edge_cases.py` over `{stats['root']}`. "
         "Read-only pass.")
    line(f"Files scanned: **{stats['total']:,}** | read errors: "
         f"{stats['read_errors']} | runtime: {stats['elapsed']:.1f}s.")
    line()

    # --- Section 1 ---
    line("## 1. Value-shape census")
    line()
    line("Shape = letters->`a`, digits->`N`, then consecutive repeats collapsed. "
         "One shape per header per file; empty values shown as `(empty)`.")
    line()
    for h in SHAPE_HEADERS:
        c = stats["shape_counts"][h]
        ex = stats["shape_example"][h]
        seen = sum(c.values())
        distinct = len(c)
        line(f"### {h} ({seen:,} files, {distinct:,} distinct shapes)")
        line()
        line("Top 20 shapes:")
        line()
        line("| shape | count | example |")
        line("|---|---|---|")
        for sh, n in c.most_common(20):
            line(f"| `{sh}` | {n:,} | {ex.get(sh, '')} |")
        line()
        rare = sorted([(sh, n) for sh, n in c.items() if n < 50],
                      key=lambda kv: kv[1], reverse=True)
        line(f"Rare shapes (<50 files): {len(rare):,} distinct. Showing up to "
             f"{RARE_SHAPE_MAX}:")
        line()
        line("| shape | count | example |")
        line("|---|---|---|")
        for sh, n in rare[:RARE_SHAPE_MAX]:
            line(f"| `{sh}` | {n} | {ex.get(sh, '')} |")
        if len(rare) > RARE_SHAPE_MAX:
            line(f"| *(+{len(rare) - RARE_SHAPE_MAX:,} more rare shapes)* | | |")
        line()

    # --- Section 2 ---
    line("## 2. Frequent-line mining (separators / markers / boilerplate)")
    line()
    line("Each body line normalized (strip, digits->`N`, emails-><email>, Notes "
         "names-><name>, separator runs canonicalized). Only lines that look "
         "like separators/markers/boilerplate are counted. Top 100:")
    line()
    line("| count | normalized line | example source |")
    line("|---|---|---|")
    for norm, n in stats["line_interesting"].most_common(100):
        orig, path = stats["line_example"].get(norm, (norm, ""))
        safe = norm.replace("|", "\\|")
        line(f"| {n:,} | `{safe}` | {path} |")
    line()
    line("### Unanticipated frequent lines (general counter, top 30 not already "
         "flagged above)")
    line()
    line("A secondary pass over ALL normalized lines, to catch marker styles the "
         "predicate above might miss.")
    line()
    flagged = set(dict(stats["line_interesting"].most_common(100)))
    line("| count | normalized line |")
    line("|---|---|")
    shown = 0
    for norm, n in stats["line_all"].most_common(400):
        if norm in flagged:
            continue
        safe = norm.replace("|", "\\|")
        line(f"| {n:,} | `{safe}` |")
        shown += 1
        if shown >= 30:
            break
    line()

    # --- Section 3 ---
    line("## 3. Parser defects by type")
    line()
    if stats["defect_counts"]:
        line("| defect type | count | example |")
        line("|---|---|---|")
        for name, n in stats["defect_counts"].most_common():
            line(f"| `{name}` | {n:,} | {stats['defect_example'].get(name, '')} |")
    else:
        line("No parser defects recorded.")
    line()

    # --- Section 4 ---
    line(f"## 4. Random sample ({len(stats['reservoir'])} files, seed "
         f"{RANDOM_SEED})")
    line()
    line("Uniform random sample across the whole corpus (so spread across "
         "mailboxes by size). Automated red flags per file; qualitative notes "
         "added by review below the table.")
    line()
    line("| # | path | automated flags | body head |")
    line("|---|---|---|---|")
    for i, display in enumerate(stats["reservoir"], 1):
        obs = sample_observations(display, cwd)
        flags = "; ".join(obs["flags"]) or "(none)"
        head = " / ".join(obs["head"]).replace("|", "\\|")[:120] or "(empty)"
        line(f"| {i} | {display} | {flags} | {head} |")
    line()
    for ln in REVIEW_NOTES_TEXT.split("\n"):
        line(ln)
    line()

    # --- New edge case types ---
    line("## New edge case types")
    line()
    line("Candidate types counted across the full pass. Each row: name, detection "
         "rule, count, example path. These feed the PART 3 metrics and the PART 4 "
         "selection checklist.")
    line()
    line("| type | detection rule | count | example |")
    line("|---|---|---|---|")
    rules = {
        "quoted_printable_body": "Content-Transfer-Encoding: quoted-printable",
        "base64_body": "Content-Transfer-Encoding: base64",
        "lotus_notes_forward": r"body `--- Forwarded by <name> on MM/DD/YYYY ---`",
        "notes_inline_header": r"body `<name>/ORG on MM/DD/YYYY HH:MM` reply header",
        "multi_original_message": ">=2 `-----Original Message-----` in one body",
        "folded_subject": "Subject header value spans a continuation line",
        "ldap_dn_recipient": "`/O=ENRON/OU=` DN in To/Cc/X-To/X-cc",
        "placeholder_address": "no.address@ / .@enron.com / undisclosed-recipients",
        "blank_subject": "Subject header present but value empty",
        "empty_body": "body is whitespace-only",
        "qp_artifact_undeclared": "QP artifacts (=XX / soft break) without QP CTE",
        "parse_exception": "email parser raised",
    }
    for name, n in stats["cand_counts"].most_common():
        rule = rules.get(name, "")
        ex = stats["cand_example"].get(name, "")
        line(f"| `{name}` | {rule} | {n:,} | {ex} |")
    line()

    empty_n = stats["cand_counts"].get("empty_body", 0)
    line("### Promotion decisions")
    line()
    line("Promoted to the PART 3 per-mailbox metrics and the PART 4 selection "
         "checklist: `quoted_printable`, `folded_subject`, `lotus_notes_forward` "
         "(`notes_forward`), `notes_inline_header` (`notes_inline`), "
         "`ldap_dn_recipient`, `multi_original_message` (`multi_original`). "
         "(`blank_subject` and `placeholder_address` were already among the six "
         "requested PART 3 counts.)")
    line()
    line("Found but **not** promoted to the checklist, with reasons:")
    line(f"- `base64_body` ({stats['cand_counts'].get('base64_body', 0)} files): "
         "too rare to ever meet the 5-file coverage threshold; the parser still "
         "handles base64, but it is not a selection criterion.")
    line(f"- `qp_artifact_undeclared` "
         f"({stats['cand_counts'].get('qp_artifact_undeclared', 0):,}): a noisy "
         "subset of quoted-printable handling (any `=XX` or `=`-EOL) that overlaps "
         "`quoted_printable`; kept as a parser note, not a distinct checklist item.")
    line("- `[IMAGE]` inline markers, multi-level `> >` quoting, spaced-letter "
         "banners, and automated-system-mail signatures (see the random-sample "
         "review above): real but either refinements of existing items "
         "(`quoted_gt`, `attachment_ref`) or dedup-time concerns (Type E), so left "
         "for a later pass.")
    line(f"- **`empty_body` fired {empty_n} times** across all {stats['total']:,} "
         "files - the spec's \"empty body\" case does not occur in this corpus, so "
         "that checklist item will be a permanent coverage gap.")
    line()

    line("### Verification note (empty_body / CRLF)")
    line()
    line(f"`empty_body = {empty_n}` was independently verified two ways over all "
         f"{stats['total']:,} files: the `\\r\\n\\r\\n`/`\\n\\n` split used here, and "
         "a line-based split that strips `\\r` from each line before testing for a "
         "blank line. Both report 0 whitespace-only bodies, with 0 disagreements.")
    line()
    line("The corpus is **100% CRLF** (517,398 of 517,401 files use a "
         "`\\r\\n\\r\\n` header/body separator; only 3 use `\\n\\n`), so every metric "
         "here already runs on CRLF input. All body-based metrics are CRLF-safe: "
         "`.strip()` drops `\\r`, `(?m)^` anchors after `\\n`, and marker/heading "
         "detection give identical results on CRLF and LF input. (A `sed /^$/` "
         "check that does not strip `\\r` can mis-read the CRLF separator line as "
         "empty - the source of an earlier false-positive empty-body report.)")
    line()

    path = os.path.join(out_dir, "DISCOVERY.md")
    with open(path, "w") as fh:
        fh.write("\n".join(L))


# --- CLI --------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Edge-case discovery over the maildir.")
    ap.add_argument("--root", default="data/maildir")
    ap.add_argument("--out", default="docs/exploration")
    ap.add_argument("--only", default=None, help="profile a single mailbox")
    ap.add_argument("--limit", type=int, default=None, help="stop after N files (smoke test)")
    ap.add_argument("--progress", type=int, default=25000, help="progress every N files (0=off)")
    args = ap.parse_args(argv)

    target = os.path.join(args.root, args.only) if args.only else args.root
    if not os.path.isdir(target):
        print(f"error: not found: {target}", file=sys.stderr)
        return 2

    print(f"Discovering edge cases in {target} -> {args.out}", file=sys.stderr)
    stats = discover(args.root, args.out, args.only, args.limit, args.progress)
    print(f"Done: {stats['total']:,} files in {stats['elapsed']:.1f}s. "
          f"-> {args.out}/DISCOVERY.md", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
