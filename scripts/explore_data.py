#!/usr/bin/env python3
"""
explore_data.py - Read-only profiler for the Enron maildir.

See docs/DECISIONS.md D-009 for why this exists. This script does NOT build any
part of the pipeline. It walks data/maildir once and writes a human-readable
picture of the raw data so the mailbox subset (D-010) and the parser/dedup
design can be chosen with real quirks in mind.

Guarantees:
  * Standard library only (Python 3.10+).
  * Read-only: it never writes inside data/.
  * Crash-proof per file: every file is wrapped in try/except. A file that
    cannot be read or parsed is COUNTED and its path RECORDED, never fatal.

Outputs (under --out, default docs/exploration/):
  REPORT.md          dataset totals, a per-mailbox table, a section per quirk
                     (counts + short excerpts), mailbox candidate sets, surprises
  mailbox_stats.csv  one row per mailbox, all numeric metrics
  examples.json      quirk category -> up to 5 real file paths (future fixtures)

The 11 metrics requested map to the code as follows:
   1 file + per-folder counts        -> mb[...]['files'], folder_basenames
   2 header presence / other headers -> header_names, PRESENCE_KEYS
   3 folded To/Cc headers            -> folded_headers()
   4 recipient counts (median/max)   -> mb_tocounts via email.utils.getaddresses
   5 Date shapes + parse failures    -> date_shapes, parsedate_to_datetime
   6 body markers                    -> RE_ORIG / RE_FWD / RE_QUOTE / RE_ATTACH ...
   7 Message-ID uniqueness           -> msgid_first / msgid_dupes
   8 exact body duplicates           -> body_first / body_dupes (blake2b of body)
   9 X-Folder vs real folder         -> xfolder_mismatch_raw / _norm
  10 parser struggles                -> parse_exception / parse_defects / missing
  11 candidate "headings"            -> heading_allcaps / _colon / _underline

PART 3 additions (D-010/D-011): six requested per-mailbox counts (empty_body,
blank_subject, placeholder_address, calendar_folder, large_to, bcc_equals_cc),
two selection-checklist support columns (missing_to, cc_present), and the new
edge case types confirmed by scripts/discover_edge_cases.py (quoted_printable,
folded_subject, notes_forward, notes_inline, ldap_dn_recipient, multi_original).
examples.json now draws each quirk's examples from many mailboxes, not the first.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

# --- Constants --------------------------------------------------------------

# compat32 is the lenient legacy policy: it almost never raises on bad input and
# records problems on msg.defects instead, which is exactly what we want for a
# "what's broken out there?" survey.
COMPAT32 = policy.compat32

# Headers the assignment names explicitly (lowercased). Anything else a file
# carries is collected under "other headers".
KNOWN_HEADERS = [
    "message-id", "date", "from", "to", "cc", "bcc", "subject",
    "x-from", "x-to", "x-cc", "x-bcc", "x-folder", "x-origin",
    "content-type", "content-transfer-encoding", "mime-version",
]

# A record is unusable without these, so a file missing any of them is a
# "parser struggle" (metric 10). body/source_file aren't headers, so excluded.
MANDATORY = ["message-id", "date", "from", "to", "subject"]

# Headers we track presence of per-mailbox for the completeness table.
PRESENCE_KEYS = ["message-id", "date", "from", "to", "cc", "bcc", "subject"]

# Folders the prompt asked to call out by name.
NAMED_FOLDERS = [
    "all_documents", "discussion_threads", "sent", "_sent_mail",
    "sent_items", "inbox", "deleted_items",
]

# Every byte 0..127; used to cheaply test for any non-ASCII byte in a file.
ASCII_BYTES = bytes(range(128))

# Only scan the first N body lines for headings so a pathological huge file
# can't dominate runtime. Enron bodies are almost always far shorter.
MAX_HEADING_LINES = 400

# Density-ranked candidate sets only consider mailboxes with at least this many
# files, so a tiny mailbox with one odd message can't top a per-file ranking.
CAND_MIN_FILES = 1000

# --- Regexes (run over the latin-1 decoded body) ----------------------------

RE_DIGIT = re.compile(r"\d")
RE_ORIG = re.compile(r"-{2,}\s*Original Message\s*-{2,}", re.I)
RE_FWD = re.compile(r"Forwarded by", re.I)
RE_QUOTE = re.compile(r"(?m)^\s*>")                 # any line starting with >
RE_ATTACH = re.compile(r"<<[^<>\r\n]{1,100}>>")     # e.g. <<report.xls>>
RE_WS_UNDERSCORE = re.compile(r"[\s_]+")

# --- PART 3 additions: new edge case detectors ------------------------------
# These back the six requested per-mailbox counts plus the new edge case types
# confirmed by the discovery pass (scripts/discover_edge_cases.py, D-011).

# Lotus Notes forward banner, e.g. "----- Forwarded by X/HOU/ECT on 03/19/2001 ---"
# (distinct from RE_ORIG, which is the Outlook "-----Original Message-----" style).
RE_NOTES_FWD = re.compile(r"-{3,}\s*Forwarded by .+? on \d{1,2}/\d{1,2}/\d{2,4}", re.I)
# Notes inline reply header, e.g. "David Oxley/ENRON@enronXgate on 03/19/2001 12:16 PM".
RE_NOTES_INLINE = re.compile(
    r"(?m)^\s*\S.*?/(?:HOU|ECT|ENRON|NA|EES|EU|Corp)\S*\s+on\s+"
    r"\d{1,2}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2}")
# LDAP/Notes distinguished name sitting in a recipient header instead of an address.
RE_LDAP_DN = re.compile(r"/O=ENRON/OU=", re.I)
# Placeholder "addresses": the real forms in this corpus (the spec's underscore
# variant is matched too, harmlessly).
RE_PLACEHOLDER = re.compile(
    r"(no[_.]?address@enron\.com|(?<![\w.])\.@enron\.com|"
    r"undisclosed[ ._\-]?recipients)", re.I)

# A To field with more than this many recipients is a distribution-list blast.
LARGE_TO_THRESHOLD = 100


# --- Small helpers ----------------------------------------------------------

def find_header_body_split(raw: bytes):
    """Return (index, sep_len) of the first blank line separating headers from
    body, handling both \\n\\n and \\r\\n\\r\\n. Returns (None, 0) if none."""
    candidates = []
    for sep in (b"\r\n\r\n", b"\n\n"):
        i = raw.find(sep)
        if i != -1:
            candidates.append((i, len(sep)))
    if not candidates:
        return None, 0
    return min(candidates)


def folded_headers(header_text: str) -> set[str]:
    """Return the set of header names (lowercased) whose value is folded across
    more than one physical line. A continuation line starts with space/tab."""
    folded: set[str] = set()
    current = None
    for line in header_text.split("\n"):
        line = line.rstrip("\r")
        if not line:
            current = None
            continue
        if line[0] in " \t":           # continuation of previous header
            if current:
                folded.add(current)
            continue
        if ":" in line:
            name = line.split(":", 1)[0].strip().lower()
            current = name if (name and " " not in name) else None
        else:
            current = None
    return folded


def grab_header_block(header_text: str, name: str) -> str:
    """Pull one header and its continuation lines out of the raw header text,
    for use as a short excerpt in the report."""
    out = []
    capturing = False
    prefix = (name + ":").lower()
    for line in header_text.split("\n"):
        bare = line.rstrip("\r")
        if not capturing and bare[: len(prefix)].lower() == prefix:
            capturing = True
            out.append(bare)
            continue
        if capturing:
            if bare[:1] in " \t":
                out.append(bare)
            else:
                break
    return "\n".join(out)


def norm_folder(s: str) -> str:
    """Normalize a folder name for comparison: lowercase, drop spaces/_."""
    return RE_WS_UNDERSCORE.sub("", s.lower())


def pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):.1f}%" if d else "n/a"


# --- Main profiler ----------------------------------------------------------

def profile(root: str, out_dir: str, only: str | None, progress_every: int):
    cwd = os.getcwd()
    start_root = root
    walk_target = os.path.join(root, only) if only else root

    # Per-mailbox accumulators.
    mb: dict[str, defaultdict] = defaultdict(lambda: defaultdict(int))
    mb_folders: dict[str, set] = defaultdict(set)      # distinct dirs per mailbox
    mb_tocounts: dict[str, list] = defaultdict(list)   # To-address counts

    # Dataset-wide accumulators.
    header_names = Counter()      # files carrying each header name
    date_shapes = Counter()       # digit-masked Date templates
    charsets = Counter()          # declared charsets
    folder_basenames = Counter()  # files per folder basename
    examples = defaultdict(list)  # quirk -> up to 5 paths (finalized from below)
    # quirk -> {mailbox: first_path}. Keeping one path per mailbox (up to 25
    # mailboxes) lets us finalize examples.json with files from MANY mailboxes
    # instead of whichever mailbox os.walk happened to visit first.
    examples_by_mb = defaultdict(dict)
    excerpts = defaultdict(list)  # quirk -> up to a few short raw snippets
    read_errors = []              # (path, error)

    # Message-ID uniqueness (metric 7). Keep memory low: a str->path map for
    # ids seen once; promote to a small dupes map only on collision.
    msgid_first: dict[str, str] = {}
    msgid_dupes: dict[str, dict] = {}

    # Exact body duplicates (metric 8). Same idea: body_first holds the single
    # occurrences; body_dupes holds only bodies seen in >1 file, with the full
    # set of directories and mailboxes they span (sets are NOT capped, so the
    # same/cross-folder split is exact; only the example path list is capped).
    body_first: dict[bytes, tuple] = {}   # hash -> (display_path, dir_key, mailbox)
    body_dupes: dict[bytes, dict] = {}    # hash -> {count, dirs, mb, paths}

    # The maildir root relative to cwd (e.g. "data/maildir"), used to recover a
    # file's mailbox from its display path so add_example can diversify.
    root_rel = os.path.relpath(start_root, cwd)

    def add_example(cat: str, path: str):
        """Record one example path per quirk, spread across mailboxes: keep the
        first file seen for each of up to 25 mailboxes; examples.json later takes
        5 of them, so a quirk is illustrated from several mailboxes not one."""
        d = examples_by_mb[cat]
        try:
            mb_name = os.path.relpath(path, root_rel).split(os.sep)[0]
        except ValueError:
            mb_name = path
        if mb_name not in d and len(d) < 25:
            d[mb_name] = path

    def add_excerpt(cat: str, text: str, cap: int = 4):
        lst = excerpts[cat]
        if len(lst) < cap:
            snippet = text.strip()
            if len(snippet) > 200:
                snippet = snippet[:200] + " ..."
            lst.append(snippet)

    total = 0
    t0 = time.perf_counter()

    for dirpath, _dirnames, filenames in os.walk(walk_target):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel_root = os.path.relpath(full, start_root)   # allen-p/inbox/1
            parts = rel_root.split(os.sep)
            mailbox = parts[0]
            dir_key = os.sep.join(parts[:-1])              # allen-p/inbox
            folder_base = parts[-2] if len(parts) >= 2 else "(root)"
            display = os.path.relpath(full, cwd)           # data/maildir/allen-p/inbox/1

            total += 1
            mb[mailbox]["files"] += 1
            mb_folders[mailbox].add(dir_key)
            folder_basenames[folder_base] += 1

            # files under any folder named "calendar" (PART 3 metric 4). Checks
            # every folder component, not just the leaf, since calendar items can
            # sit in nested subfolders.
            if any(p.lower() == "calendar" for p in parts[1:-1]):
                mb[mailbox]["calendar_folder"] += 1
                add_example("calendar", display)

            if progress_every and total % progress_every == 0:
                rate = total / (time.perf_counter() - t0)
                print(f"  ...{total:,} files ({rate:,.0f}/s)", file=sys.stderr)

            # --- read raw bytes (metric: read errors) ---
            try:
                with open(full, "rb") as fh:
                    raw = fh.read()
            except Exception as e:  # unreadable file: count, record, move on
                read_errors.append((display, repr(e)))
                mb[mailbox]["read_errors"] += 1
                add_example("read_error", display)
                continue

            # --- non-ASCII bytes (metric 6) ---
            if raw.translate(None, ASCII_BYTES):
                mb[mailbox]["non_ascii"] += 1
                add_example("non_ascii", display)

            # --- split header / body on the first blank line ---
            hidx, hlen = find_header_body_split(raw)
            if hidx is None:
                header_bytes, body_bytes = raw, b""
            else:
                header_bytes, body_bytes = raw[:hidx], raw[hidx + hlen:]
            header_text = header_bytes.decode("latin-1", "replace")
            body_text = body_bytes.decode("latin-1", "replace")

            # empty body: only whitespace after the headers (PART 3 metric 1)
            if not body_bytes.strip():
                mb[mailbox]["empty_body"] += 1
                add_example("empty_body", display)

            # --- exact body duplicates (metric 8) ---
            # Skip empty/whitespace-only bodies: they'd form one meaningless
            # giant "duplicate" group.
            if body_bytes.strip():
                h = hashlib.blake2b(body_bytes, digest_size=16).digest()
                if h in body_dupes:
                    g = body_dupes[h]
                    g["count"] += 1
                    g["dirs"].add(dir_key)
                    g["mb"].add(mailbox)
                    if len(g["paths"]) < 5:
                        g["paths"].append(display)
                    mb[mailbox]["dup_files"] += 1
                elif h in body_first:
                    first_disp, first_dir, first_mb = body_first.pop(h)
                    body_dupes[h] = {
                        "count": 2,
                        "dirs": {first_dir, dir_key},
                        "mb": {first_mb, mailbox},
                        "paths": [first_disp, display],
                    }
                    mb[mailbox]["dup_files"] += 1
                    mb[first_mb]["dup_files"] += 1
                else:
                    body_first[h] = (display, dir_key, mailbox)

            # --- parse headers with the email module (metric 10) ---
            msg = None
            try:
                msg = BytesParser(policy=COMPAT32).parsebytes(raw)
            except Exception as e:
                mb[mailbox]["parse_exception"] += 1
                add_example("parse_struggle_exception", display)
                add_excerpt("parse_struggle_exception", f"{display}: {e!r}")

            if msg is not None:
                keyset = {k.lower() for k in msg.keys()}
                header_names.update(keyset)

                # defects = things the parser noticed but tolerated
                if msg.defects:
                    mb[mailbox]["parse_defects"] += 1
                    add_example("parse_defect", display)
                    add_excerpt("parse_defect",
                                f"{display}: {[type(d).__name__ for d in msg.defects]}")

                # per-mailbox header presence (metric 2)
                for key in PRESENCE_KEYS:
                    if key in keyset:
                        mb[mailbox]["has_" + key.replace("-", "_")] += 1

                # missing mandatory headers (metric 10)
                missing = [k for k in MANDATORY if k not in keyset]
                if missing:
                    mb[mailbox]["missing_mandatory"] += 1
                    add_example("missing_mandatory_header", display)
                    add_excerpt("missing_mandatory_header",
                                f"{display}: missing {missing}")

                # missing the To header specifically (checklist item missing_to).
                # Tracked apart from missing_mandatory so selection can threshold
                # on it directly (it dominates missing_mandatory in practice).
                if "to" not in keyset:
                    mb[mailbox]["missing_to"] += 1
                    add_example("missing_to", display)

                # recipient count in To (metric 4)
                if "to" in keyset:
                    to_vals = msg.get_all("To", [])
                    addrs = [a for (_n, a) in getaddresses(to_vals) if a]
                    mb_tocounts[mailbox].append(len(addrs))
                    if len(addrs) > LARGE_TO_THRESHOLD:        # PART 3 metric 5
                        mb[mailbox]["large_to"] += 1
                        add_example("large_to", display)

                # Date shape + parse failures (metric 5)
                dval = msg["Date"]
                if dval:
                    date_shapes[RE_DIGIT.sub("N", dval.strip())] += 1
                    try:
                        dt = parsedate_to_datetime(dval)
                        if dt is None:
                            raise ValueError("parsed to None")
                    except Exception:
                        mb[mailbox]["date_fail"] += 1
                        add_example("date_parse_failure", display)
                        add_excerpt("date_parse_failure", dval)

                # content-type / charset / multipart (metric 6)
                ctype = (msg.get_content_type() or "").lower()
                cs = msg.get_content_charset()
                if cs:
                    charsets[cs.lower()] += 1
                if ctype.startswith("multipart"):
                    mb[mailbox]["multipart"] += 1
                    add_example("multipart", display)

                # X-Folder vs real folder (metric 9)
                xf = msg["X-Folder"]
                if xf:
                    mb[mailbox]["xfolder_present"] += 1
                    last = xf.replace("/", "\\").split("\\")[-1].strip()
                    if last.lower() != folder_base.lower():
                        mb[mailbox]["xfolder_mismatch_raw"] += 1
                    if norm_folder(last) != norm_folder(folder_base):
                        mb[mailbox]["xfolder_mismatch_norm"] += 1
                        add_example("xfolder_mismatch", display)
                        add_excerpt("xfolder_mismatch",
                                    f"folder={folder_base!r}  X-Folder last={last!r}")

                # Message-ID uniqueness (metric 7)
                mid = msg["Message-ID"]
                if mid:
                    mid = mid.strip()
                    if mid in msgid_dupes:
                        d = msgid_dupes[mid]
                        d["count"] += 1
                        if len(d["paths"]) < 5:
                            d["paths"].append(display)
                    elif mid in msgid_first:
                        first = msgid_first.pop(mid)
                        msgid_dupes[mid] = {"count": 2, "paths": [first, display]}
                        add_example("msgid_duplicate", first)
                        add_example("msgid_duplicate", display)
                    else:
                        msgid_first[mid] = display

                # --- PART 3 new edge case types (need the parsed message) ---

                # blank Subject: header present but value empty (metric 2)
                subj = msg["Subject"]
                if subj is not None and str(subj).strip() == "":
                    mb[mailbox]["blank_subject"] += 1
                    add_example("blank_subject", display)

                # placeholder addresses in From/To/Cc (metric 3)
                addr_blob = " ".join(str(msg.get(h) or "") for h in ("From", "To", "Cc"))
                if RE_PLACEHOLDER.search(addr_blob):
                    mb[mailbox]["placeholder_address"] += 1
                    add_example("placeholder_address", display)

                # LDAP/Notes distinguished name sitting in a recipient header
                # instead of an email address (new type from discovery).
                recip_blob = " ".join(
                    str(msg.get(h) or "") for h in ("To", "Cc", "X-To", "X-cc"))
                if RE_LDAP_DN.search(recip_blob):
                    mb[mailbox]["ldap_dn_recipient"] += 1
                    add_example("ldap_dn_recipient", display)

                # Bcc exactly equals Cc (metric 6): identical, non-empty address
                # sets. Enron often blind-copied the same list it CC'd.
                cc_set = {a for (_n, a) in getaddresses(msg.get_all("Cc", [])) if a}
                bcc_set = {a for (_n, a) in getaddresses(msg.get_all("Bcc", [])) if a}
                if cc_set and cc_set == bcc_set:
                    mb[mailbox]["bcc_equals_cc"] += 1
                    add_example("bcc_equals_cc", display)

                # quoted-printable body (new type): needs decoding before parsing
                cte = (msg["Content-Transfer-Encoding"] or "").strip().lower()
                if cte == "quoted-printable":
                    mb[mailbox]["quoted_printable"] += 1
                    add_example("quoted_printable", display)

            # --- folded To/Cc from the raw header text (metric 3) ---
            fset = folded_headers(header_text)
            if "to" in fset:
                mb[mailbox]["folded_to"] += 1
                add_example("folded_to_cc", display)
                add_excerpt("folded_to_cc", grab_header_block(header_text, "To"))
            if "cc" in fset:
                mb[mailbox]["folded_cc"] += 1
                add_example("folded_to_cc", display)
            if "subject" in fset:                              # new type: folded Subject
                mb[mailbox]["folded_subject"] += 1
                add_example("folded_subject", display)

            # --- body markers (metric 6) ---
            origs = RE_ORIG.findall(body_text)
            if origs:
                mb[mailbox]["marker_original"] += 1
                add_example("marker_original_message", display)
                if len(origs) >= 2:              # nested forward chain (new type)
                    mb[mailbox]["multi_original"] += 1
                    add_example("multi_original", display)
            if RE_FWD.search(body_text):
                mb[mailbox]["marker_forwarded"] += 1
                add_example("marker_forwarded", display)
            if RE_NOTES_FWD.search(body_text):                 # Lotus Notes forward banner
                mb[mailbox]["notes_forward"] += 1
                add_example("notes_forward", display)
            if RE_NOTES_INLINE.search(body_text):              # Notes inline reply header
                mb[mailbox]["notes_inline"] += 1
                add_example("notes_inline", display)
            if RE_QUOTE.search(body_text):
                mb[mailbox]["quoted"] += 1
                add_example("quoted_gt", display)
            m = RE_ATTACH.search(body_text)
            if m:
                mb[mailbox]["attachment_ref"] += 1
                add_example("attachment_ref", display)
                add_excerpt("attachment_ref", m.group())

            # --- candidate "headings" (metric 11) ---
            lines = body_text.split("\n")[:MAX_HEADING_LINES]
            found_caps = found_colon = found_under = False
            for i, line in enumerate(lines):
                s = line.strip()
                if not s:
                    continue
                # a content line followed by a rule of --- or ===
                if i + 1 < len(lines):
                    nxt = lines[i + 1].strip()
                    if (len(nxt) >= 3 and set(nxt) <= {"-", "="}
                            and not set(s) <= {"-", "="}):
                        found_under = True
                        add_excerpt("heading_underline", s)
                alpha = [c for c in s if c.isalpha()]
                if alpha and 3 <= len(s) <= 60:
                    if all(c.isupper() for c in alpha) and len(alpha) / len(s) >= 0.5:
                        found_caps = True
                        add_excerpt("heading_allcaps", s)
                if 2 <= len(s) <= 60 and s.endswith(":") and any(c.isalpha() for c in s):
                    found_colon = True
                    add_excerpt("heading_colon", s)
            if found_caps:
                mb[mailbox]["heading_allcaps"] += 1
                add_example("heading_allcaps", display)
            if found_colon:
                mb[mailbox]["heading_colon"] += 1
                add_example("heading_colon", display)
            if found_under:
                mb[mailbox]["heading_underline"] += 1
                add_example("heading_underline", display)

    elapsed = time.perf_counter() - t0

    # Finalize per-mailbox derived metrics.
    for name, d in mb.items():
        d["folders"] = len(mb_folders[name])
        tos = mb_tocounts.get(name, [])
        d["median_to"] = int(statistics.median(tos)) if tos else 0
        d["max_to"] = max(tos) if tos else 0
        d["edge_score"] = (
            d["date_fail"] + d["missing_mandatory"] + d["parse_defects"]
            + d["parse_exception"] + d["folded_to"] + d["folded_cc"]
            + d["multipart"] + d["non_ascii"] + d["attachment_ref"]
        )

    # Finalize examples.json: up to 5 paths per quirk, each from a DIFFERENT
    # mailbox (the dict preserves first-seen order), so fixtures span the corpus
    # instead of clustering in whichever mailbox os.walk visited first.
    for cat, dd in examples_by_mb.items():
        examples[cat] = list(dd.values())[:5]

    # Body-duplicate fixtures (metric 8). A single path can't demonstrate a
    # duplicate, so for each flavour we grab one representative group's member
    # paths (2-5 files that share an identical body).
    _dup_cats = (
        "body_duplicate_cross_mailbox",   # body appears in >1 mailbox
        "body_duplicate_cross_folder",    # >1 folder, within one mailbox
        "body_duplicate_same_folder",     # >1 file, all in one directory
    )
    for g in body_dupes.values():
        paths = g["paths"][:5]
        if len(paths) < 2:
            continue
        if len(g["mb"]) > 1:
            cat = "body_duplicate_cross_mailbox"
        elif len(g["dirs"]) > 1:
            cat = "body_duplicate_cross_folder"
        else:
            cat = "body_duplicate_same_folder"
        if cat not in examples:
            examples[cat] = paths
        if all(c in examples for c in _dup_cats):
            break

    stats = {
        "total": total,
        "elapsed": elapsed,
        "mb": mb,
        "mb_tocounts": mb_tocounts,
        "header_names": header_names,
        "date_shapes": date_shapes,
        "charsets": charsets,
        "folder_basenames": folder_basenames,
        "examples": examples,
        "excerpts": excerpts,
        "read_errors": read_errors,
        "msgid_first": msgid_first,
        "msgid_dupes": msgid_dupes,
        "body_first": body_first,
        "body_dupes": body_dupes,
    }

    os.makedirs(out_dir, exist_ok=True)
    write_csv(out_dir, stats)
    write_examples(out_dir, stats)
    write_report(out_dir, stats, root)
    return stats


# --- Output writers ---------------------------------------------------------

CSV_FIELDS = [
    "mailbox", "files", "folders",
    "pct_message_id", "pct_date", "pct_from", "pct_to", "pct_cc", "pct_bcc",
    "pct_subject",
    "folded_to", "folded_cc", "median_to", "max_to",
    "date_fail", "missing_mandatory", "parse_defects", "parse_exception",
    "read_errors",
    "marker_original", "marker_forwarded", "quoted", "attachment_ref",
    "multipart", "non_ascii",
    "xfolder_present", "xfolder_mismatch_raw", "xfolder_mismatch_norm",
    "heading_allcaps", "heading_colon", "heading_underline",
    "dup_files", "edge_score",
    # --- PART 3 additions (6 requested counts + checklist support + new types) ---
    "missing_to", "large_to", "cc_present", "bcc_equals_cc",
    "empty_body", "blank_subject", "placeholder_address", "calendar_folder",
    "quoted_printable", "folded_subject", "notes_forward", "notes_inline",
    "ldap_dn_recipient", "multi_original",
]


def _row(name: str, d: dict) -> dict:
    files = d["files"]

    def rate(key):
        return pct(d.get("has_" + key, 0), files)

    return {
        "mailbox": name, "files": files, "folders": d["folders"],
        "pct_message_id": rate("message_id"), "pct_date": rate("date"),
        "pct_from": rate("from"), "pct_to": rate("to"), "pct_cc": rate("cc"),
        "pct_bcc": rate("bcc"), "pct_subject": rate("subject"),
        "folded_to": d["folded_to"], "folded_cc": d["folded_cc"],
        "median_to": d["median_to"], "max_to": d["max_to"],
        "date_fail": d["date_fail"], "missing_mandatory": d["missing_mandatory"],
        "parse_defects": d["parse_defects"], "parse_exception": d["parse_exception"],
        "read_errors": d["read_errors"],
        "marker_original": d["marker_original"],
        "marker_forwarded": d["marker_forwarded"], "quoted": d["quoted"],
        "attachment_ref": d["attachment_ref"], "multipart": d["multipart"],
        "non_ascii": d["non_ascii"], "xfolder_present": d["xfolder_present"],
        "xfolder_mismatch_raw": d["xfolder_mismatch_raw"],
        "xfolder_mismatch_norm": d["xfolder_mismatch_norm"],
        "heading_allcaps": d["heading_allcaps"],
        "heading_colon": d["heading_colon"],
        "heading_underline": d["heading_underline"],
        "dup_files": d["dup_files"], "edge_score": d["edge_score"],
        # PART 3 additions. cc_present reuses the has_cc presence counter.
        "missing_to": d["missing_to"], "large_to": d["large_to"],
        "cc_present": d.get("has_cc", 0), "bcc_equals_cc": d["bcc_equals_cc"],
        "empty_body": d["empty_body"], "blank_subject": d["blank_subject"],
        "placeholder_address": d["placeholder_address"],
        "calendar_folder": d["calendar_folder"],
        "quoted_printable": d["quoted_printable"],
        "folded_subject": d["folded_subject"],
        "notes_forward": d["notes_forward"], "notes_inline": d["notes_inline"],
        "ldap_dn_recipient": d["ldap_dn_recipient"],
        "multi_original": d["multi_original"],
    }


def write_csv(out_dir: str, stats: dict):
    path = os.path.join(out_dir, "mailbox_stats.csv")
    rows = sorted(stats["mb"].items(), key=lambda kv: kv[1]["files"], reverse=True)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for name, d in rows:
            w.writerow(_row(name, d))


def write_examples(out_dir: str, stats: dict):
    path = os.path.join(out_dir, "examples.json")
    out = {cat: paths for cat, paths in sorted(stats["examples"].items())}
    with open(path, "w") as fh:
        json.dump(out, fh, indent=2)


def pick_set(mb: dict, key_fn, min_n=5, min_total=10000):
    """Greedily take mailboxes (highest key first) until we have at least
    min_n of them AND at least min_total emails. Returns (names, total)."""
    ordered = sorted(mb.items(), key=lambda kv: key_fn(kv[1]), reverse=True)
    chosen, total = [], 0
    for name, d in ordered:
        chosen.append(name)
        total += d["files"]
        if len(chosen) >= min_n and total >= min_total:
            break
    return chosen, total


def write_report(out_dir: str, stats: dict, root: str):
    mb = stats["mb"]
    total = stats["total"]
    L = []  # lines

    def line(s=""):
        L.append(s)

    # ---- dataset totals ----
    def dsum(key):
        return sum(d[key] for d in mb.values())

    all_tos = [n for lst in stats["mb_tocounts"].values() for n in lst]
    body_dupes = stats["body_dupes"]
    dup_groups = len(body_dupes)
    dup_files = sum(g["count"] for g in body_dupes.values())
    same_folder_groups = sum(1 for g in body_dupes.values() if len(g["dirs"]) == 1)
    cross_folder_groups = sum(1 for g in body_dupes.values() if len(g["dirs"]) > 1)
    cross_mb_groups = sum(1 for g in body_dupes.values() if len(g["mb"]) > 1)
    unique_ids = len(stats["msgid_first"]) + len(stats["msgid_dupes"])
    dup_ids = len(stats["msgid_dupes"])
    dup_id_extra = sum(g["count"] - 1 for g in stats["msgid_dupes"].values())

    line("# Enron maildir - exploration report")
    line()
    line(f"Generated by `scripts/explore_data.py` over `{root}`. Read-only pass.")
    line(f"Runtime: {stats['elapsed']:.1f}s.")
    line()
    line("## Dataset totals")
    line()
    line(f"- Mailboxes: **{len(mb)}**")
    line(f"- Files scanned: **{total:,}**")
    line(f"- Read errors: {len(stats['read_errors'])}")
    line(f"- Parser exceptions: {dsum('parse_exception')} | "
         f"files with defects: {dsum('parse_defects')} | "
         f"missing a mandatory header: {dsum('missing_mandatory')}")
    line(f"- Folded To headers: {dsum('folded_to')} | folded Cc: {dsum('folded_cc')}")
    if all_tos:
        line(f"- To recipients: median **{int(statistics.median(all_tos))}**, "
             f"max **{max(all_tos):,}** (across files that have a To header)")
    line(f"- Date parse failures: {dsum('date_fail')}")
    line(f"- Bodies with non-ASCII bytes: {dsum('non_ascii')}")
    line(f"- Body markers: Original-Message {dsum('marker_original'):,} | "
         f"Forwarded-by {dsum('marker_forwarded'):,} | "
         f"quoted(>) {dsum('quoted'):,} | "
         f"attachment-refs {dsum('attachment_ref'):,} | "
         f"multipart {dsum('multipart'):,}")
    line(f"- Message-IDs: {unique_ids:,} unique | **{dup_ids:,}** IDs reused "
         f"across {dup_id_extra:,} extra files")
    line(f"- Exact-body duplicate groups: **{dup_groups:,}** covering "
         f"{dup_files:,} files "
         f"(same-folder {same_folder_groups:,} | cross-folder {cross_folder_groups:,} "
         f"| cross-mailbox {cross_mb_groups:,})")
    line(f"- X-Folder present: {dsum('xfolder_present'):,} | "
         f"disagrees with real folder: raw {dsum('xfolder_mismatch_raw'):,}, "
         f"normalized {dsum('xfolder_mismatch_norm'):,}")
    line(f"- Candidate headings (files with >=1): ALL-CAPS {dsum('heading_allcaps'):,} | "
         f"ends-with-colon {dsum('heading_colon'):,} | "
         f"underlined {dsum('heading_underline'):,}")
    line(f"- PART 3 counts: empty-body {dsum('empty_body'):,} | "
         f"blank-subject {dsum('blank_subject'):,} | "
         f"placeholder-addr {dsum('placeholder_address'):,} | "
         f"calendar-folder {dsum('calendar_folder'):,} | "
         f"large-To(>{LARGE_TO_THRESHOLD}) {dsum('large_to'):,} | "
         f"Bcc==Cc {dsum('bcc_equals_cc'):,} | missing-To {dsum('missing_to'):,}")
    line(f"- Marker styles: quoted-printable {dsum('quoted_printable'):,} | "
         f"Notes-forward {dsum('notes_forward'):,} | "
         f"Notes-inline-header {dsum('notes_inline'):,} | "
         f"folded-Subject {dsum('folded_subject'):,} | "
         f"LDAP-DN-recipient {dsum('ldap_dn_recipient'):,} | "
         f"nested-Original-Message {dsum('multi_original'):,}")
    line()

    # ---- named folders ----
    line("### Folder counts (named folders + top others)")
    line()
    line("| folder | files |")
    line("|---|---|")
    fb = stats["folder_basenames"]
    for f in NAMED_FOLDERS:
        line(f"| {f} | {fb.get(f, 0):,} |")
    line("| *--- other top folders ---* | |")
    named = set(NAMED_FOLDERS)
    for f, c in fb.most_common():
        if f not in named:
            line(f"| {f} | {c:,} |")
            if c < 500:
                break
    line()

    # ---- per-mailbox table ----
    line("## Per-mailbox table (key metrics)")
    line()
    line("Full metrics for every mailbox are in `mailbox_stats.csv`.")
    line()
    line("| mailbox | files | folders | %msgid | %date | %to | med_to | max_to "
         "| date_fail | dup_files | xf_mismatch |")
    line("|---|---|---|---|---|---|---|---|---|---|---|")
    for name, d in sorted(mb.items(), key=lambda kv: kv[1]["files"], reverse=True):
        files = d["files"]
        line(f"| {name} | {files:,} | {d['folders']} | "
             f"{pct(d.get('has_message_id', 0), files)} | "
             f"{pct(d.get('has_date', 0), files)} | "
             f"{pct(d.get('has_to', 0), files)} | "
             f"{d['median_to']} | {d['max_to']:,} | {d['date_fail']} | "
             f"{d['dup_files']:,} | {d['xfolder_mismatch_norm']:,} |")
    line()

    # ---- per-quirk sections with excerpts ----
    ex = stats["excerpts"]

    def quirk(title, count_desc, excerpt_key=None, note=None):
        line(f"### {title}")
        line()
        line(count_desc)
        if note:
            line()
            line(note)
        if excerpt_key and ex.get(excerpt_key):
            line()
            line("Excerpts:")
            line()
            for s in ex[excerpt_key][:3]:
                line("```")
                line(s)
                line("```")
        line()

    line("## Quirks in detail")
    line()
    quirk("3. Folded (multi-line) To/Cc headers",
          f"Folded To in {dsum('folded_to'):,} files, folded Cc in "
          f"{dsum('folded_cc'):,} files.",
          "folded_to_cc",
          note="A naive line-by-line header reader would truncate these. "
               "Must unfold before splitting recipients.")
    quirk("5. Date parse failures",
          f"{dsum('date_fail')} files whose Date `parsedate_to_datetime` "
          f"could not parse.",
          "date_parse_failure")
    quirk("6. Attachment references in body",
          f"{dsum('attachment_ref'):,} files contain `<<filename>>` style refs.",
          "attachment_ref")
    quirk("9. X-Folder vs real folder path",
          f"X-Folder present in {dsum('xfolder_present'):,} files; the last "
          f"segment disagrees with the real folder in "
          f"{dsum('xfolder_mismatch_norm'):,} files (normalized).",
          "xfolder_mismatch",
          note="Expected: X-Folder shows Outlook display names (e.g. "
               "'Sent Items') while the dir is 'sent_items' or '_sent_mail'. "
               "Do not trust X-Folder as the true folder.")
    quirk("10. Parser struggles",
          f"Exceptions {dsum('parse_exception')} | defects "
          f"{dsum('parse_defects')} | missing mandatory header "
          f"{dsum('missing_mandatory')}.",
          "missing_mandatory_header")
    quirk("11. Candidate 'headings'",
          f"Files with >=1: ALL-CAPS {dsum('heading_allcaps'):,} | "
          f"ends-with-colon {dsum('heading_colon'):,} | underlined "
          f"{dsum('heading_underline'):,}.",
          note="The spec's 'headings' field is vague. Sample hits below so a "
               "concrete definition can be chosen later.")
    for key, label in [("heading_allcaps", "ALL-CAPS"),
                       ("heading_colon", "ends-with-colon"),
                       ("heading_underline", "underlined")]:
        if ex.get(key):
            line(f"_{label} examples:_ " + " / ".join(f"`{s}`" for s in ex[key][:3]))
            line()

    # ---- date shapes ----
    line("### Date header shapes (digits -> N), top 15")
    line()
    line("| shape | count |")
    line("|---|---|")
    for shape, c in stats["date_shapes"].most_common(15):
        line(f"| `{shape}` | {c:,} |")
    line()

    # ---- charsets + other headers ----
    line("### Declared charsets")
    line()
    line(", ".join(f"{cs} ({c:,})" for cs, c in stats["charsets"].most_common(10))
         or "none")
    line()
    line("### Other header names seen (not in the spec list)")
    line()
    known = set(KNOWN_HEADERS)
    others = [(h, c) for h, c in stats["header_names"].most_common() if h not in known]
    line(", ".join(f"`{h}` ({c:,})" for h, c in others[:25]) or "none")
    line()

    # ---- candidate mailbox sets ----
    line("## Mailbox selection candidates")
    line()
    line("Three candidate sets, each >=5 mailboxes and >=10,000 emails. "
         "**I am not choosing** - this is for the D-010 decision.")
    line()

    # Absolute-count ranking scales with mailbox size, so B and C would just
    # echo A. The density variants (per-file rate, among mailboxes with
    # >=CAND_MIN_FILES files) surface genuinely different, still-usable sets.
    eligible = {k: v for k, v in mb.items() if v["files"] >= CAND_MIN_FILES}

    vol_set, vol_total = pick_set(mb, lambda d: d["files"])
    edge_abs_set, edge_abs_total = pick_set(mb, lambda d: d["edge_score"])
    dup_abs_set, dup_abs_total = pick_set(mb, lambda d: d["dup_files"])
    edge_den_set, edge_den_total = pick_set(
        eligible, lambda d: d["edge_score"] / d["files"])
    dup_den_set, dup_den_total = pick_set(
        eligible, lambda d: d["dup_files"] / d["files"])

    line(f"**A. Largest volume** ({vol_total:,} emails): {', '.join(vol_set)}")
    line("- Tradeoff: most data and most duplicates to find, but slowest to "
         "ingest and dominated by a few huge mailboxes.")
    line()
    line("**B. Most edge cases**")
    line(f"- by count ({edge_abs_total:,} emails): {', '.join(edge_abs_set)}")
    line(f"- by rate, files>={CAND_MIN_FILES:,} ({edge_den_total:,} emails): "
         f"{', '.join(edge_den_set)}")
    line("- edge_score = folding + date fails + defects + multipart + non-ASCII "
         "+ attachment refs + missing headers.")
    line("- Tradeoff: best stress test for the parser; the by-rate set is less "
         "dominated by sheer size but mixes in smaller mailboxes.")
    line()
    line("**C. Most cross-folder duplication**")
    line(f"- by count ({dup_abs_total:,} emails): {', '.join(dup_abs_set)}")
    line(f"- by rate, files>={CAND_MIN_FILES:,} ({dup_den_total:,} emails): "
         f"{', '.join(dup_den_set)}")
    line("- dup_files = files whose exact body appears in >1 file anywhere; in "
         "this corpus every such group is cross-folder (same-folder = 0).")
    line("- Tradeoff: best exercise for dedup logic; the by-count set overlaps "
         "A since big mailboxes naturally hold more dups.")
    line()

    # ---- surprises ----
    line("## Surprises (things that would break a naive parser or dedup)")
    line()
    surprises = []
    if dup_ids:
        surprises.append(
            f"**Message-IDs are NOT unique**: {dup_ids:,} IDs reused across "
            f"{dup_id_extra:,} extra files. A UNIQUE constraint on message_id "
            "will reject real rows (same mail saved in inbox + all_documents). "
            "Dedup/insert must expect this.")
    if dsum("folded_to"):
        surprises.append(
            f"Folded To/Cc headers ({dsum('folded_to'):,}+ files): recipients "
            "span multiple lines; splitting on the first line loses addresses.")
    if cross_folder_groups:
        surprises.append(
            f"{cross_folder_groups:,} byte-identical bodies appear across "
            "different folders - the Enron habit of filing one mail in several "
            "folders. Exact-body dedup must key on more than body, or it will "
            "merge legitimately-distinct filings.")
    if dsum("xfolder_mismatch_norm"):
        surprises.append(
            "X-Folder rarely matches the real folder name, so it cannot be "
            "used as ground truth for folder.")
    if all_tos and max(all_tos) > 50:
        surprises.append(
            f"To fields reach {max(all_tos):,} recipients - watch for huge "
            "normalized recipient tables and distribution-list blasts.")
    if dsum("date_fail"):
        surprises.append(
            f"{dsum('date_fail')} Date headers fail stdlib parsing; timezone "
            "handling will need a fallback.")
    surprises.append(
        "Empty-body files were excluded from exact-dup detection on purpose "
        "(they would otherwise form one giant false group).")
    for s in surprises:
        line(f"- {s}")
    line()

    path = os.path.join(out_dir, "REPORT.md")
    with open(path, "w") as fh:
        fh.write("\n".join(L))


# --- CLI --------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only Enron maildir profiler.")
    ap.add_argument("--root", default="data/maildir",
                    help="maildir root (default: data/maildir)")
    ap.add_argument("--out", default="docs/exploration",
                    help="output dir (default: docs/exploration)")
    ap.add_argument("--only", default=None,
                    help="profile a single mailbox (for testing)")
    ap.add_argument("--progress", type=int, default=20000,
                    help="print progress every N files (0=silent)")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.root):
        print(f"error: root not found: {args.root}", file=sys.stderr)
        return 2

    target = os.path.join(args.root, args.only) if args.only else args.root
    if not os.path.isdir(target):
        print(f"error: mailbox not found: {target}", file=sys.stderr)
        return 2

    print(f"Profiling {target} -> {args.out}", file=sys.stderr)
    stats = profile(args.root, args.out, args.only, args.progress)
    print(f"Done: {stats['total']:,} files in {stats['elapsed']:.1f}s. "
          f"Outputs in {args.out}/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
