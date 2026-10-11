# Mailbox selection - candidate sets

Built by `scripts/select_mailboxes.py` from `mailbox_stats.csv` (coverage) and a raw-file duplicate probe over the candidate pool. See D-010 / D-011. **No final set is chosen here - that is D-012.**

Thresholds: item covered at >= 5 files (real_resend at >= 20 groups); each set must parse to >= 12,000 records; E-hint at >= 10 identical from+subject.
Probe read 70,475 raw files across 15 pool mailboxes in 28.6s.

## The 5 size bands

Mailboxes sorted ascending by file count, split into quintiles (band 1 = smallest).

| band | mailboxes | file-count range |
|---|---|---|
| 1 | 30 | 35 - 719 |
| 2 | 30 | 721 - 1,291 |
| 3 | 30 | 1,293 - 2,237 |
| 4 | 30 | 2,249 - 4,542 |
| 5 | 30 | 4,685 - 28,465 |

## Set 1 - max coverage (greedy)

Pure greedy: one per band, each pick maximizing newly-covered checklist items (tie-break larger file count).

| mailbox | band | files | missing_mandatory | est. parsed |
|---|---|---|---|---|
| ring-a | 1 | 706 | 43 | 663 |
| ybarbo-p | 2 | 1,291 | 116 | 1,175 |
| presto-k | 3 | 2,204 | 70 | 2,134 |
| baughman-d | 4 | 2,760 | 122 | 2,638 |
| beck-s | 5 | 11,830 | 275 | 11,555 |
| **total** | | **18,791** | | **18,165** OK |

### Coverage (counts per mailbox; covered = set total meets threshold)

| checklist item | ring-a | ybarbo-p | presto-k | baughman-d | beck-s | total | covered |
|---|---|---|---|---|---|---|---|
| folded_to | 90 | 433 | 496 | 1084 | 2434 | 4537 | yes |
| folded_cc | 30 | 254 | 114 | 125 | 1597 | 2120 | yes |
| missing_to | 43 | 116 | 70 | 122 | 275 | 626 | yes |
| large_to | 11 | 9 | 39 | 193 | 84 | 336 | yes |
| has_cc | 46 | 480 | 406 | 425 | 4220 | 5577 | yes |
| bcc_equals_cc | 46 | 480 | 406 | 425 | 4220 | 5577 | yes |
| original_message | 123 | 223 | 964 | 522 | 927 | 2759 | yes |
| forwarded_by | 288 | 160 | 66 | 301 | 3080 | 3895 | yes |
| quoted_gt | 94 | 20 | 35 | 202 | 103 | 454 | yes |
| attachment_ref | 19 | 43 | 145 | 166 | 164 | 537 | yes |
| non_ascii | 1 | 7 | 0 | 0 | 14 | 22 | yes |
| calendar | 0 | 8 | 13 | 29 | 126 | 176 | yes |
| empty_body | 0 | 0 | 0 | 0 | 0 | 0 | **NO** |
| blank_subject | 53 | 7 | 59 | 60 | 137 | 316 | yes |
| placeholder_address | 23 | 62 | 63 | 29 | 23 | 200 | yes |
| parse_defect | 0 | 0 | 0 | 5 | 0 | 5 | yes |
| quoted_printable | 28 | 57 | 79 | 203 | 553 | 920 | yes |
| folded_subject | 26 | 71 | 123 | 127 | 273 | 620 | yes |
| lotus_notes_forward | 287 | 76 | 64 | 295 | 2966 | 3688 | yes |
| notes_inline_header | 287 | 162 | 69 | 291 | 3048 | 3857 | yes |
| ldap_dn_recipient | 178 | 648 | 1651 | 1246 | 1356 | 5079 | yes |
| multi_original_message | 22 | 79 | 405 | 171 | 289 | 966 | yes |
| real_resend (Type C groups) | 19 | 20 | 31 | 23 | 33 | 68 | yes |

Covered: 22/23. **Gaps: empty_body.**

### Duplicate probe

- Type A (same mailbox, different folders, same date): **3,992** groups
- Type B (different mailboxes, same date): **164** groups
- Type C (same from+subject+body, different dates = real resend): **68** groups
- Type E hint (same from+subject >= 10x, likely automated): **103** groups

Type C per mailbox (what dedup actually works on): ring-a 19, ybarbo-p 20, presto-k 31, baughman-d 23, beck-s 33

## Set 2 - minimize cross-mailbox overlap (low Type B)

Greedy but each pick after the first minimizes shared-body overlap with the already-chosen mailboxes, to keep Type B (cross-mailbox storage copies) low.

| mailbox | band | files | missing_mandatory | est. parsed |
|---|---|---|---|---|
| gilbertsmith-d | 1 | 578 | 24 | 554 |
| schwieger-j | 2 | 738 | 112 | 626 |
| whalley-g | 3 | 1,878 | 460 | 1,418 |
| mcconnell-m | 4 | 4,542 | 185 | 4,357 |
| beck-s | 5 | 11,830 | 275 | 11,555 |
| **total** | | **19,566** | | **18,510** OK |

### Coverage (counts per mailbox; covered = set total meets threshold)

| checklist item | gilbertsmith-d | schwieger-j | whalley-g | mcconnell-m | beck-s | total | covered |
|---|---|---|---|---|---|---|---|
| folded_to | 116 | 137 | 584 | 484 | 2434 | 3755 | yes |
| folded_cc | 64 | 30 | 278 | 181 | 1597 | 2150 | yes |
| missing_to | 24 | 112 | 460 | 185 | 275 | 1056 | yes |
| large_to | 12 | 15 | 51 | 7 | 84 | 169 | yes |
| has_cc | 176 | 97 | 524 | 940 | 4220 | 5957 | yes |
| bcc_equals_cc | 176 | 97 | 524 | 940 | 4220 | 5957 | yes |
| original_message | 144 | 146 | 257 | 437 | 927 | 1911 | yes |
| forwarded_by | 28 | 117 | 95 | 1096 | 3080 | 4416 | yes |
| quoted_gt | 11 | 45 | 88 | 116 | 103 | 363 | yes |
| attachment_ref | 46 | 17 | 106 | 116 | 164 | 449 | yes |
| non_ascii | 0 | 0 | 2 | 0 | 14 | 16 | yes |
| calendar | 32 | 13 | 248 | 46 | 126 | 465 | yes |
| empty_body | 0 | 0 | 0 | 0 | 0 | 0 | **NO** |
| blank_subject | 17 | 26 | 59 | 125 | 137 | 364 | yes |
| placeholder_address | 6 | 51 | 36 | 28 | 23 | 144 | yes |
| parse_defect | 0 | 5 | 6 | 0 | 0 | 11 | yes |
| quoted_printable | 26 | 26 | 69 | 243 | 553 | 917 | yes |
| folded_subject | 26 | 38 | 145 | 102 | 273 | 584 | yes |
| lotus_notes_forward | 28 | 116 | 90 | 1048 | 2966 | 4248 | yes |
| notes_inline_header | 24 | 113 | 102 | 1075 | 3048 | 4362 | yes |
| ldap_dn_recipient | 312 | 272 | 477 | 467 | 1356 | 2884 | yes |
| multi_original_message | 52 | 41 | 59 | 138 | 289 | 579 | yes |
| real_resend (Type C groups) | 5 | 7 | 8 | 6 | 23 | 35 | yes |

Covered: 22/23. **Gaps: empty_body.**

### Duplicate probe

- Type A (same mailbox, different folders, same date): **4,758** groups
- Type B (different mailboxes, same date): **147** groups
- Type C (same from+subject+body, different dates = real resend): **35** groups
- Type E hint (same from+subject >= 10x, likely automated): **120** groups

Type C per mailbox (what dedup actually works on): gilbertsmith-d 5, schwieger-j 7, whalley-g 8, mcconnell-m 6, beck-s 23

## Set 3 - force a storage-copy pair (high Type B)

Forces the highest-overlap cross-band pair into the set so the storage-copy collapse step has real Type B work, then fills the rest greedily for coverage.

Forced pair: **presto-k** (band 3) & **sturm-f** (band 2) share 173 identical bodies.

| mailbox | band | files | missing_mandatory | est. parsed |
|---|---|---|---|---|
| ring-a | 1 | 706 | 43 | 663 |
| sturm-f | 2 | 1,169 | 46 | 1,123 |
| presto-k | 3 | 2,204 | 70 | 2,134 |
| baughman-d | 4 | 2,760 | 122 | 2,638 |
| beck-s | 5 | 11,830 | 275 | 11,555 |
| **total** | | **18,669** | | **18,113** OK |

### Coverage (counts per mailbox; covered = set total meets threshold)

| checklist item | ring-a | sturm-f | presto-k | baughman-d | beck-s | total | covered |
|---|---|---|---|---|---|---|---|
| folded_to | 90 | 227 | 496 | 1084 | 2434 | 4331 | yes |
| folded_cc | 30 | 50 | 114 | 125 | 1597 | 1916 | yes |
| missing_to | 43 | 46 | 70 | 122 | 275 | 556 | yes |
| large_to | 11 | 58 | 39 | 193 | 84 | 385 | yes |
| has_cc | 46 | 141 | 406 | 425 | 4220 | 5238 | yes |
| bcc_equals_cc | 46 | 141 | 406 | 425 | 4220 | 5238 | yes |
| original_message | 123 | 201 | 964 | 522 | 927 | 2737 | yes |
| forwarded_by | 288 | 91 | 66 | 301 | 3080 | 3826 | yes |
| quoted_gt | 94 | 5 | 35 | 202 | 103 | 439 | yes |
| attachment_ref | 19 | 21 | 145 | 166 | 164 | 515 | yes |
| non_ascii | 1 | 0 | 0 | 0 | 14 | 15 | yes |
| calendar | 0 | 11 | 13 | 29 | 126 | 179 | yes |
| empty_body | 0 | 0 | 0 | 0 | 0 | 0 | **NO** |
| blank_subject | 53 | 58 | 59 | 60 | 137 | 367 | yes |
| placeholder_address | 23 | 34 | 63 | 29 | 23 | 172 | yes |
| parse_defect | 0 | 0 | 0 | 5 | 0 | 5 | yes |
| quoted_printable | 28 | 23 | 79 | 203 | 553 | 886 | yes |
| folded_subject | 26 | 31 | 123 | 127 | 273 | 580 | yes |
| lotus_notes_forward | 287 | 91 | 64 | 295 | 2966 | 3703 | yes |
| notes_inline_header | 287 | 87 | 69 | 291 | 3048 | 3782 | yes |
| ldap_dn_recipient | 178 | 475 | 1651 | 1246 | 1356 | 4906 | yes |
| multi_original_message | 22 | 63 | 405 | 171 | 289 | 950 | yes |
| real_resend (Type C groups) | 16 | 13 | 24 | 21 | 23 | 54 | yes |

Covered: 22/23. **Gaps: empty_body.**

### Duplicate probe

- Type A (same mailbox, different folders, same date): **4,065** groups
- Type B (different mailboxes, same date): **227** groups
- Type C (same from+subject+body, different dates = real resend): **54** groups
- Type E hint (same from+subject >= 10x, likely automated): **105** groups

Type C per mailbox (what dedup actually works on): ring-a 16, sturm-f 13, presto-k 24, baughman-d 21, beck-s 23

## Tradeoffs

- **Set 1** covers 22/23 items - the most edge cases. Type B = 164 groups.
- **Set 2** covers 22/23 items with the lowest cross-mailbox overlap (Type B = 147). Best if you want mailboxes that are mostly independent.
- **Set 3** covers 22/23 items and deliberately includes a high-overlap pair (Type B = 227), so the cross-mailbox storage-copy collapse gets tested.

- Note: all three sets share **beck-s** - the greedy ranks the pool by edge-case breadth (items covered), not raw size, so the broadest-coverage mailbox wins its band in every set. (This is why the band-5 pick is not one of the 28k-file mailboxes: those have fewer than 5 non-ASCII files, so they cover one fewer item.) Because that mailbox is shared, it also caps how far the sets' Type B counts can diverge.
- Note: `empty_body` is the single uncovered item in every set - the discovery pass found **0** empty-body files across all 517,401 files, so no set can cover it. This is a property of the corpus, not a selection failure.

All three exceed the 12,000 estimated-parsed floor. The decision (D-012) is a tradeoff between maximum edge-case coverage (Set 1), clean independent mailboxes (Set 2), and exercising cross-mailbox dedup (Set 3).
