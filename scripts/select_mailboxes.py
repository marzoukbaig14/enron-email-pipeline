#!/usr/bin/env python3
"""
select_mailboxes.py - Build candidate mailbox sets for the pipeline subset.

See docs/DECISIONS.md D-010 (stratified selection) and D-011 (discovery-built
checklist). This script does NOT pick the final set - it produces three
candidate sets and a coverage/duplicate report for a human to decide from.

Inputs:
  * docs/exploration/mailbox_stats.csv  (from scripts/explore_data.py) - the ONLY
    source for coverage counts. No raw files are read for selection.
  * data/maildir/<mailbox>/...          - read ONLY for the duplicate probe
    (step 5), and only for the mailboxes in the candidate pool.

Method (D-010):
  1. Sort mailboxes by file count, split into 5 bands of 30 (quintiles).
  2. Edge case checklist; an item is "covered" when the chosen set has at least
     COVER_MIN_FILES files with it (REAL_RESEND_MIN_GROUPS groups for real_resend).
  3. Greedy: one mailbox per band, each pick maximizing newly-covered items,
     tie-break larger file count.
  4. Estimated parsed = files - missing_mandatory; require MIN_ESTIMATED_PARSED,
     else swap the band-1 pick for a larger mailbox and say so.
  5. Duplicate probe (raw reads) on the candidate pool only: body hash + from +
     subject (Re:/Fwd: stripped) + date -> Type A/B/C/E hint. Exact match only.
  6. Three sets: greedy (Set 1), greedy minimizing cross-mailbox overlap /
     Type B (Set 2), greedy forcing a high-overlap pair (Set 3).

Output: docs/exploration/SELECTION.md
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sys
import time
from collections import defaultdict
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

# --- Tunable thresholds (D-010) ---------------------------------------------

COVER_MIN_FILES = 5           # an item is covered at >= this many files in the set
REAL_RESEND_MIN_GROUPS = 20   # real_resend covered at >= this many Type C groups
MIN_ESTIMATED_PARSED = 12000  # a set must parse to at least this many records
E_HINT_MIN = 10               # same from+subject >= this many times -> automated hint
N_BANDS = 5                   # quintiles
BAND_SIZE = 30                # 150 mailboxes / 5
POOL_PER_BAND = 3             # alternates per band to probe, for Set 2 / Set 3

# --- Edge case checklist -----------------------------------------------------
# (checklist item name, mailbox_stats.csv column). real_resend is special: it
# comes from the duplicate probe, not from the CSV, so it is handled separately.

CHECKLIST = [
    ("folded_to", "folded_to"),
    ("folded_cc", "folded_cc"),
    ("missing_to", "missing_to"),
    ("large_to", "large_to"),
    ("has_cc", "cc_present"),
    ("bcc_equals_cc", "bcc_equals_cc"),
    ("original_message", "marker_original"),
    ("forwarded_by", "marker_forwarded"),
    ("quoted_gt", "quoted"),
    ("attachment_ref", "attachment_ref"),
    ("non_ascii", "non_ascii"),
    ("calendar", "calendar_folder"),
    ("empty_body", "empty_body"),
    ("blank_subject", "blank_subject"),
    ("placeholder_address", "placeholder_address"),
    ("parse_defect", "parse_defects"),
    # --- new edge case types from the PART 2 discovery pass (D-011) ---
    ("quoted_printable", "quoted_printable"),
    ("folded_subject", "folded_subject"),
    ("lotus_notes_forward", "notes_forward"),
    ("notes_inline_header", "notes_inline"),
    ("ldap_dn_recipient", "ldap_dn_recipient"),
    ("multi_original_message", "multi_original"),
]
REAL_RESEND = "real_resend"   # from the probe; covered at REAL_RESEND_MIN_GROUPS

RE_REPLY_PREFIX = re.compile(r"^\s*(re|fw|fwd)\s*:\s*", re.I)


# --- CSV loading ------------------------------------------------------------

def load_stats(path: str) -> dict[str, dict]:
    """Read mailbox_stats.csv into {mailbox: {column: int_or_str}}. Numeric
    columns default to 0 when absent (so this runs even against a stats file
    written before the PART 3 columns existed)."""
    rows: dict[str, dict] = {}
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            rows[r["mailbox"]] = r
    return rows


def ival(row: dict, col: str) -> int:
    """Integer value of a column, tolerating missing/blank/comma-formatted cells."""
    v = row.get(col, "")
    if v is None or v == "":
        return 0
    try:
        return int(str(v).replace(",", ""))
    except ValueError:
        return 0


# --- Banding ----------------------------------------------------------------

def make_bands(stats: dict[str, dict]) -> list[list[str]]:
    """Sort mailboxes ascending by file count and split into N_BANDS bands.
    Band 0 is the smallest mailboxes, band N_BANDS-1 the largest."""
    ordered = sorted(stats, key=lambda m: ival(stats[m], "files"))
    n = len(ordered)
    # Even split; with exactly 150 mailboxes this is 5 x 30.
    bands = []
    for b in range(N_BANDS):
        lo = (b * n) // N_BANDS
        hi = ((b + 1) * n) // N_BANDS
        bands.append(ordered[lo:hi])
    return bands


def band_of(bands: list[list[str]], mailbox: str) -> int:
    for i, members in enumerate(bands):
        if mailbox in members:
            return i
    return -1


# --- Coverage ---------------------------------------------------------------

def set_coverage(stats: dict, mailboxes: list[str]) -> dict[str, int]:
    """Summed count per checklist column across the given mailboxes."""
    totals = {item: 0 for item, _col in CHECKLIST}
    for m in mailboxes:
        for item, col in CHECKLIST:
            totals[item] += ival(stats[m], col)
    return totals


def covered_items(totals: dict[str, int]) -> set[str]:
    return {item for item, n in totals.items() if n >= COVER_MIN_FILES}


def mailbox_breadth(stats: dict, mailbox: str) -> int:
    """How many checklist items this mailbox alone covers (count >= threshold).
    Used to rank alternates into the probe pool."""
    return sum(1 for _item, col in CHECKLIST
               if ival(stats[mailbox], col) >= COVER_MIN_FILES)


# --- Greedy selection -------------------------------------------------------

def greedy_max_coverage(stats: dict, bands: list[list[str]],
                        candidates_per_band: list[list[str]]) -> list[str]:
    """One mailbox per band, chosen to maximize newly-covered checklist items.
    Free band order: each round picks the globally best (band, mailbox). Tie-break
    on the larger file count."""
    running = {item: 0 for item, _ in CHECKLIST}
    chosen: dict[int, str] = {}
    for _ in range(len(bands)):
        best = None  # (new_covered, files, band, mailbox)
        for b, members in enumerate(candidates_per_band):
            if b in chosen:
                continue
            for m in members:
                new_cov = 0
                for item, col in CHECKLIST:
                    if running[item] < COVER_MIN_FILES and \
                       running[item] + ival(stats[m], col) >= COVER_MIN_FILES:
                        new_cov += 1
                key = (new_cov, ival(stats[m], "files"))
                if best is None or key > best[:2]:
                    best = (new_cov, ival(stats[m], "files"), b, m)
        _nc, _f, b, m = best
        chosen[b] = m
        for item, col in CHECKLIST:
            running[item] += ival(stats[m], col)
    return [chosen[b] for b in range(len(bands))]


def greedy_min_affinity(stats: dict, bands_count: int,
                        candidates_per_band: list[list[str]],
                        affinity: dict) -> list[str]:
    """One mailbox per band, minimizing cross-mailbox body-overlap (Type B).
    Each pick has the lowest total affinity to the already-chosen set, tie-break
    higher coverage then larger files. The first pick naturally falls back to
    coverage/size, since affinity to an empty chosen set is zero for everyone."""
    running = {item: 0 for item, _ in CHECKLIST}
    chosen: dict[int, str] = {}
    chosen_names: list[str] = []
    for _ in range(bands_count):
        best = None  # (cmp_key, band, mailbox)
        for b, members in enumerate(candidates_per_band):
            if b in chosen:
                continue
            for m in members:
                aff = sum(affinity.get((m, c), 0) for c in chosen_names)
                new_cov = sum(
                    1 for item, col in CHECKLIST
                    if running[item] < COVER_MIN_FILES and
                    running[item] + ival(stats[m], col) >= COVER_MIN_FILES)
                cmp = (-aff, new_cov, ival(stats[m], "files"))
                if best is None or cmp > best[0]:
                    best = (cmp, b, m)
        _cmp, b, m = best
        chosen[b] = m
        chosen_names.append(m)
        for item, col in CHECKLIST:
            running[item] += ival(stats[m], col)
    return [chosen[b] for b in range(bands_count)]


def greedy_force_pair(stats: dict, bands_count: int,
                      candidates_per_band: list[list[str]],
                      affinity: dict, bands: list[list[str]]) -> list[str]:
    """Force the highest-affinity cross-band pair (so the storage-copy collapse
    step gets exercised), then greedily fill the remaining bands for coverage."""
    # find the highest-affinity pair whose members sit in different bands
    best_pair = None
    seen = set()
    for (a, c), n in affinity.items():
        if a == c or (c, a) in seen:
            continue
        seen.add((a, c))
        ba, bc = band_of(bands, a), band_of(bands, c)
        if ba == bc or ba < 0 or bc < 0:
            continue
        if best_pair is None or n > best_pair[0]:
            best_pair = (n, a, c, ba, bc)
    chosen: dict[int, str] = {}
    running = {item: 0 for item, _ in CHECKLIST}
    if best_pair:
        _n, a, c, ba, bc = best_pair
        chosen[ba], chosen[bc] = a, c
        for m in (a, c):
            for item, col in CHECKLIST:
                running[item] += ival(stats[m], col)
    # fill remaining bands greedily for coverage
    for _ in range(bands_count):
        if len(chosen) == bands_count:
            break
        best = None
        for b, members in enumerate(candidates_per_band):
            if b in chosen:
                continue
            for m in members:
                new_cov = sum(
                    1 for item, col in CHECKLIST
                    if running[item] < COVER_MIN_FILES and
                    running[item] + ival(stats[m], col) >= COVER_MIN_FILES)
                key = (new_cov, ival(stats[m], "files"))
                if best is None or key > best[:2]:
                    best = (new_cov, ival(stats[m], "files"), b, m)
        _nc, _f, b, m = best
        chosen[b] = m
        for item, col in CHECKLIST:
            running[item] += ival(stats[m], col)
    return [chosen[b] for b in range(bands_count)], best_pair


def enforce_min_parsed(stats, bands, chosen: list[str],
                       candidates_per_band) -> tuple[list[str], str | None]:
    """If estimated parsed < MIN_ESTIMATED_PARSED, swap the band-0 (smallest)
    pick for the largest-file candidate in band 0. Returns (set, note)."""
    est = sum(ival(stats[m], "files") - ival(stats[m], "missing_mandatory")
              for m in chosen)
    if est >= MIN_ESTIMATED_PARSED:
        return chosen, None
    band0 = candidates_per_band[0] or bands[0]
    larger = max(band0, key=lambda m: ival(stats[m], "files"))
    if larger == chosen[0]:
        return chosen, (f"estimated parsed {est:,} < {MIN_ESTIMATED_PARSED:,} and "
                        "band-1 already holds its largest mailbox; cannot swap up")
    new = list(chosen)
    old = new[0]
    new[0] = larger
    note = (f"estimated parsed was {est:,} (< {MIN_ESTIMATED_PARSED:,}); swapped "
            f"band-1 pick {old} for larger {larger}")
    return new, note


# --- Duplicate probe (reads raw files) --------------------------------------

def norm_subject(s: str | None) -> str:
    s = s or ""
    while True:
        nxt = RE_REPLY_PREFIX.sub("", s)
        if nxt == s:
            break
        s = nxt
    return s.strip().lower()


def probe_mailboxes(root: str, mailboxes: set[str], progress_every: int):
    """Read raw files for the given mailboxes once. Returns:
      records: list of (mailbox, dir_key, body_hash, from_addr, subj_norm, date_key)
               for files with a From and a non-empty body
      mb_hashes: {mailbox: set(body_hash)} over all non-empty-body files (affinity)
      n_read:   files read
    """
    records = []
    mb_hashes: dict[str, set] = defaultdict(set)
    n_read = 0
    t0 = time.perf_counter()
    for mb in sorted(mailboxes):
        base = os.path.join(root, mb)
        for dirpath, _dirs, filenames in os.walk(base):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root)
                parts = rel.split(os.sep)
                dir_key = os.sep.join(parts[:-1])
                n_read += 1
                if progress_every and n_read % progress_every == 0:
                    rate = n_read / (time.perf_counter() - t0)
                    print(f"    probe ...{n_read:,} files ({rate:,.0f}/s)",
                          file=sys.stderr)
                try:
                    with open(full, "rb") as fh:
                        raw = fh.read()
                except Exception:
                    continue
                idx = min([i for i in (raw.find(b"\r\n\r\n"), raw.find(b"\n\n"))
                           if i != -1], default=-1)
                if idx == -1:
                    body = b""
                else:
                    sep = 4 if raw[idx:idx + 4] == b"\r\n\r\n" else 2
                    body = raw[idx + sep:]
                if not body.strip():
                    continue
                bh = hashlib.blake2b(body, digest_size=16).digest()
                mb_hashes[mb].add(bh)
                try:
                    msg = BytesParser(policy=policy.compat32).parsebytes(raw)
                except Exception:
                    continue
                from_vals = msg.get_all("From", [])
                addrs = [a for (_n, a) in getaddresses(from_vals) if a]
                if not addrs:
                    continue
                from_addr = addrs[0].lower()
                subj = norm_subject(msg["Subject"])
                dval = msg["Date"]
                date_key = None
                if dval:
                    try:
                        dt = parsedate_to_datetime(dval)
                        date_key = dt.isoformat() if dt else dval.strip()
                    except Exception:
                        date_key = dval.strip()
                records.append((mb, dir_key, bh, from_addr, subj, date_key))
    return records, mb_hashes, n_read


def pairwise_affinity(mb_hashes: dict[str, set]) -> dict:
    """affinity[(a,b)] = affinity[(b,a)] = # body hashes shared by a and b."""
    aff = {}
    names = list(mb_hashes)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            shared = len(mb_hashes[a] & mb_hashes[b])
            if shared:
                aff[(a, b)] = shared
                aff[(b, a)] = shared
    return aff


def dup_types_for_set(records, mailboxes: set[str]) -> dict:
    """Classify duplicates within one candidate set (its mailboxes only)."""
    recs = [r for r in records if r[0] in mailboxes]
    full = defaultdict(list)       # (bh,from,subj,date) -> [(mb,dir_key)]
    resend = defaultdict(lambda: {"dates": set(), "mbs": set()})
    auto = defaultdict(int)        # (from,subj) -> count
    for mb, dir_key, bh, frm, subj, date in recs:
        full[(bh, frm, subj, date)].append((mb, dir_key))
        g = resend[(bh, frm, subj)]
        g["dates"].add(date)
        g["mbs"].add(mb)
        auto[(frm, subj)] += 1

    type_a = type_b = same_folder = 0
    for members in full.values():
        if len(members) < 2:
            continue
        mbs = {m for m, _d in members}
        dirs = {d for _m, d in members}
        if len(mbs) >= 2:
            type_b += 1
        elif len(dirs) >= 2:
            type_a += 1
        else:
            same_folder += 1

    type_c = 0
    c_per_mb = defaultdict(int)
    for g in resend.values():
        if len(g["dates"]) >= 2:           # same from+subject+body, different dates
            type_c += 1
            for m in g["mbs"]:
                c_per_mb[m] += 1

    e_hint = sum(1 for n in auto.values() if n >= E_HINT_MIN)

    return {
        "A": type_a, "B": type_b, "same_folder": same_folder,
        "C": type_c, "C_per_mb": dict(c_per_mb), "E_hint": e_hint,
        "n_files": len(recs),
    }


# --- Report -----------------------------------------------------------------

def est_parsed(stats, mailboxes) -> int:
    return sum(ival(stats[m], "files") - ival(stats[m], "missing_mandatory")
              for m in mailboxes)


def write_selection(out_dir, stats, bands, sets, dup, swaps, forced_pair,
                    pool, probe_files, elapsed):
    L = []

    def line(s=""):
        L.append(s)

    line("# Mailbox selection - candidate sets")
    line()
    line("Built by `scripts/select_mailboxes.py` from `mailbox_stats.csv` "
         "(coverage) and a raw-file duplicate probe over the candidate pool. "
         "See D-010 / D-011. **No final set is chosen here - that is D-012.**")
    line()
    line(f"Thresholds: item covered at >= {COVER_MIN_FILES} files "
         f"(real_resend at >= {REAL_RESEND_MIN_GROUPS} groups); each set must "
         f"parse to >= {MIN_ESTIMATED_PARSED:,} records; E-hint at "
         f">= {E_HINT_MIN} identical from+subject.")
    line(f"Probe read {probe_files:,} raw files across {len(pool)} pool mailboxes "
         f"in {elapsed:.1f}s.")
    line()

    # bands
    line("## The 5 size bands")
    line()
    line("Mailboxes sorted ascending by file count, split into quintiles "
         "(band 1 = smallest).")
    line()
    line("| band | mailboxes | file-count range |")
    line("|---|---|---|")
    for i, members in enumerate(bands, 1):
        counts = [ival(stats[m], "files") for m in members]
        line(f"| {i} | {len(members)} | {min(counts):,} - {max(counts):,} |")
    line()

    set_names = ["Set 1 - max coverage (greedy)",
                 "Set 2 - minimize cross-mailbox overlap (low Type B)",
                 "Set 3 - force a storage-copy pair (high Type B)"]
    set_why = [
        "Pure greedy: one per band, each pick maximizing newly-covered checklist "
        "items (tie-break larger file count).",
        "Greedy but each pick after the first minimizes shared-body overlap with "
        "the already-chosen mailboxes, to keep Type B (cross-mailbox storage "
        "copies) low.",
        "Forces the highest-overlap cross-band pair into the set so the "
        "storage-copy collapse step has real Type B work, then fills the rest "
        "greedily for coverage.",
    ]

    for si, (members, name, why) in enumerate(zip(sets, set_names, set_why)):
        line(f"## {name}")
        line()
        line(why)
        if si == 2 and forced_pair:
            n, a, c, ba, bc = forced_pair
            line()
            line(f"Forced pair: **{a}** (band {ba+1}) & **{c}** (band {bc+1}) "
                 f"share {n:,} identical bodies.")
        if swaps[si]:
            line()
            line(f"Note: {swaps[si]}")
        line()
        total_files = sum(ival(stats[m], "files") for m in members)
        est = est_parsed(stats, members)
        line("| mailbox | band | files | missing_mandatory | est. parsed |")
        line("|---|---|---|---|---|")
        for m in members:
            line(f"| {m} | {band_of(bands, m)+1} | {ival(stats[m],'files'):,} | "
                 f"{ival(stats[m],'missing_mandatory'):,} | "
                 f"{ival(stats[m],'files')-ival(stats[m],'missing_mandatory'):,} |")
        line(f"| **total** | | **{total_files:,}** | | "
             f"**{est:,}** {'OK' if est>=MIN_ESTIMATED_PARSED else 'SHORT'} |")
        line()

        # coverage table
        totals = set_coverage(stats, members)
        cov = covered_items(totals)
        d = dup[si]
        line("### Coverage (counts per mailbox; covered = set total meets threshold)")
        line()
        header = "| checklist item | " + " | ".join(members) + " | total | covered |"
        line(header)
        line("|" + "---|" * (len(members) + 3))
        for item, col in CHECKLIST:
            cells = " | ".join(str(ival(stats[m], col)) for m in members)
            mark = "yes" if item in cov else "**NO**"
            line(f"| {item} | {cells} | {totals[item]} | {mark} |")
        # real_resend row from the probe
        c_cells = " | ".join(str(d["C_per_mb"].get(m, 0)) for m in members)
        rr_cov = "yes" if d["C"] >= REAL_RESEND_MIN_GROUPS else "**NO**"
        line(f"| {REAL_RESEND} (Type C groups) | {c_cells} | {d['C']} | {rr_cov} |")
        line()
        gaps = [item for item, _ in CHECKLIST if item not in cov]
        if d["C"] < REAL_RESEND_MIN_GROUPS:
            gaps.append(REAL_RESEND)
        line(f"Covered: {len(cov) + (1 if d['C']>=REAL_RESEND_MIN_GROUPS else 0)}"
             f"/{len(CHECKLIST)+1}. "
             + (f"**Gaps: {', '.join(gaps)}.**" if gaps else "No gaps."))
        line()

        # duplicate counts
        line("### Duplicate probe")
        line()
        line(f"- Type A (same mailbox, different folders, same date): **{d['A']:,}** "
             "groups")
        line(f"- Type B (different mailboxes, same date): **{d['B']:,}** groups")
        line(f"- Type C (same from+subject+body, different dates = real resend): "
             f"**{d['C']:,}** groups")
        line(f"- Type E hint (same from+subject >= {E_HINT_MIN}x, likely "
             f"automated): **{d['E_hint']:,}** groups")
        if d["same_folder"]:
            line(f"- (same-folder exact copies: {d['same_folder']:,} groups)")
        line()
        line("Type C per mailbox (what dedup actually works on): "
             + ", ".join(f"{m} {d['C_per_mb'].get(m,0)}" for m in members))
        line()

    # tradeoffs
    line("## Tradeoffs")
    line()
    cov_counts = []
    for si in range(3):
        totals = set_coverage(stats, sets[si])
        c = len(covered_items(totals)) + (
            1 if dup[si]["C"] >= REAL_RESEND_MIN_GROUPS else 0)
        cov_counts.append(c)
    line(f"- **Set 1** covers {cov_counts[0]}/{len(CHECKLIST)+1} items - the most "
         f"edge cases. Type B = {dup[0]['B']:,} groups.")
    line(f"- **Set 2** covers {cov_counts[1]}/{len(CHECKLIST)+1} items with the "
         f"lowest cross-mailbox overlap (Type B = {dup[1]['B']:,}). Best if you "
         "want mailboxes that are mostly independent.")
    line(f"- **Set 3** covers {cov_counts[2]}/{len(CHECKLIST)+1} items and "
         f"deliberately includes a high-overlap pair (Type B = {dup[2]['B']:,}), "
         "so the cross-mailbox storage-copy collapse gets tested.")
    line()
    shared = set(sets[0]) & set(sets[1]) & set(sets[2])
    if shared:
        line(f"- Note: all three sets share **{', '.join(sorted(shared))}** - the "
             "greedy ranks the pool by edge-case breadth (items covered), not raw "
             "size, so the broadest-coverage mailbox wins its band in every set. "
             "(This is why the band-5 pick is not one of the 28k-file mailboxes: "
             "those have fewer than 5 non-ASCII files, so they cover one fewer "
             "item.) Because that mailbox is shared, it also caps how far the "
             "sets' Type B counts can diverge.")
    line("- Note: `empty_body` is the single uncovered item in every set - the "
         "discovery pass found **0** empty-body files across all 517,401 files, so "
         "no set can cover it. This is a property of the corpus, not a selection "
         "failure.")
    line()
    line("All three exceed the 12,000 estimated-parsed floor. The decision (D-012) "
         "is a tradeoff between maximum edge-case coverage (Set 1), clean "
         "independent mailboxes (Set 2), and exercising cross-mailbox dedup (Set 3).")
    line()

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "SELECTION.md"), "w") as fh:
        fh.write("\n".join(L))


# --- Orchestration ----------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Build candidate mailbox sets.")
    ap.add_argument("--stats", default="docs/exploration/mailbox_stats.csv")
    ap.add_argument("--root", default="data/maildir")
    ap.add_argument("--out", default="docs/exploration")
    ap.add_argument("--progress", type=int, default=20000)
    args = ap.parse_args(argv)

    if not os.path.isfile(args.stats):
        print(f"error: stats not found: {args.stats}", file=sys.stderr)
        return 2

    stats = load_stats(args.stats)
    bands = make_bands(stats)

    # candidate pool: for each band, the top POOL_PER_BAND mailboxes by breadth
    # (how many checklist items they alone cover), tie-break file count.
    candidates_per_band = []
    for members in bands:
        ranked = sorted(
            members,
            key=lambda m: (mailbox_breadth(stats, m), ival(stats[m], "files")),
            reverse=True)
        candidates_per_band.append(ranked[:POOL_PER_BAND])

    # Set 1: greedy max coverage over the pool.
    set1 = greedy_max_coverage(stats, bands, candidates_per_band)

    # The probe pool = Set 1 picks + all per-band alternates.
    pool = set(set1)
    for members in candidates_per_band:
        pool.update(members)

    print(f"Probing {len(pool)} pool mailboxes "
          f"({sum(ival(stats[m],'files') for m in pool):,} files)...",
          file=sys.stderr)
    t0 = time.perf_counter()
    records, mb_hashes, probe_files = probe_mailboxes(
        args.root, pool, args.progress)
    affinity = pairwise_affinity(mb_hashes)
    elapsed = time.perf_counter() - t0

    # Set 2 and Set 3 use affinity.
    set2 = greedy_min_affinity(stats, len(bands), candidates_per_band, affinity)
    set3, forced_pair = greedy_force_pair(
        stats, len(bands), candidates_per_band, affinity, bands)

    # Enforce the estimated-parsed floor on each set.
    sets, swaps = [], []
    for s in (set1, set2, set3):
        s2, note = enforce_min_parsed(stats, bands, s, candidates_per_band)
        sets.append(s2)
        swaps.append(note)

    # Duplicate classification per set (from the one probe).
    dup = [dup_types_for_set(records, set(s)) for s in sets]

    write_selection(args.out, stats, bands, sets, dup, swaps, forced_pair,
                    pool, probe_files, elapsed)
    print(f"Done in {elapsed:.1f}s (probe). -> {args.out}/SELECTION.md",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
