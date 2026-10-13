# AI usage

Drafted by Claude Code from my transcripts and decision log. I reviewed and edited it.

This file covers sections 4.3 and 6 of the assignment. It is written in the first person. The sources were my Claude Code transcripts (`~/.claude/projects/`), `docs/DECISIONS.md`, `docs/plans/`, the git log and the repo files. Where a claim could not be checked against a source, I say so.

Contents: [1. Tool used](#1-tool-used) · [2. Prompting strategy](#2-prompting-strategy) · [3. Iterations and debugging](#3-iterations-and-debugging) · [4. Verification](#4-verification) · [5. What I wrote vs. what AI wrote](#5-what-i-wrote-vs-what-ai-wrote) · [6. Lessons learned](#6-lessons-learned) · [7. MCP integration](#7-mcp-integration)

## 1. Tool used

**Claude Code 2.1.296** (the version printed by `claude --version` on 2026-10-13). Earlier sessions may have run an older build. I did not record it at the time.

**Models**, read from the transcripts:

| Work | Model |
|---|---|
| Decision log setup (Oct 9) | Claude Opus 5.5 |
| Data exploration and mailbox selection (Oct 10 to 11) | Claude Opus 4.8 |
| Schema, layout, models, header parser, db, ingest, parse_body, dedup, notify, MCP sender, CLI | Claude Sonnet 5.5 |

I set the effort level to `xhigh` on Oct 10 (saved as my default).

**claude.ai** (a Project, one chat per topic) was my planning tool (D-002). I made the design decisions there and wrote them up as `D-###` entries. The chats are not in this repo.

D-005 says to `/export` every session into `notes/`. I did not keep that up, and there is no `notes/` folder. The raw transcripts are in `~/.claude/projects/` in my Codespace and are not part of the repo.

### How the work was organized

1. **Decide in claude.ai.** Structural choices (schema, approach, tradeoffs) were mine (D-004).
2. **Log the decision.** The text goes into `docs/DECISIONS.md` as an append-only `D-###` entry (D-003). A change is a new entry, never an edit.
3. **Write a plan** in `docs/plans/` when the design needs more than a decision (`module-layout.md`).
4. **Paste a prompt into Claude Code.** Every prompt starts with "read these files", then logs the decision, then builds test-first. The agent reports at the end and does not commit unless I say so.
5. **Review, then the next prompt.** I read the summary, check numbers against the data, and decide.

The sessions, in order:

| Dates | Session | What it did |
|---|---|---|
| Oct 9 | setup | `DECISIONS.md`, D-001 to D-008 |
| Oct 10 to 11 | exploration (2 sessions) | profile all 517,401 files, discovery pass, mailbox selection: D-009 to D-011, D-013 |
| Oct 11 | schema | `schema.sql` and tests: D-012 |
| Oct 11 | layout and models | `module-layout.md`, skeleton, enums: D-014, D-015 |
| Oct 11 | header parser | D-016, D-017 |
| Oct 11 to 13 | two parallel sessions | db, discover, ingest (D-018) and the `main.py` wiring; parse_body (D-019, D-020); dedup (D-021, D-023); notify (D-022), the MCP sender (D-024), the parameter-driven CLI (D-025), the notice layout (D-026); the README |
| Oct 11 | MCP test | one session that only sent the manual test email (section 7) |

The two sessions started within a minute of each other and ran in parallel. Each prompt named what the other was building and what not to touch ("Another session is building db, discover, and ingest in parallel. Don't touch those"). Both appended to `DECISIONS.md` and edited `module-layout.md`. Two decision numbers collided (D-018 became D-019, D-022 became D-023). My rule "next free number, re-read the file right before appending" handled both.

## 2. Prompting strategy

I prompted **one task at a time** and never gave the whole spec at once. The order followed the dependencies: explore the data, choose mailboxes, design the schema, plan the modules, then parsers, db and ingest, dedup, notify, the CLI, and docs. I used `/clear` between tasks so each session started from the written decisions, not from chat memory.

I counted about 40 prompts over 10 sessions. About 25 were long pasted specs. About 14 were short follow-ups ("proceed", "yes add test 15 to 17 before finishing").

Most long prompts have the same shape:

1. **Read first**, and a list of files not to touch.
2. **PART 1:** the decision text, to be logged verbatim before any code.
3. **PART 2:** the tests as a list of behaviors, "run and see them fail, then implement".
4. **A real-data check** on a fixed-seed sample, with the agent reporting what looks wrong.
5. **A summary** of tests added, files changed, judgment calls and anything unfinished. "Don't commit."

### Five example prompts

**1. A full build prompt (parse_body, 2026-10-11 16:49).** Quoted in part.

> Read CLAUDE.md, docs/assignment.md, docs/DECISIONS.md, docs/plans/module-layout.md, docs/exploration/DISCOVERY.md, and src/enron_pipeline/models.py and parse_headers.py before doing anything. Another session is building db, discover, and ingest in parallel. Don't touch those, scripts/, or docs/exploration/.
> …
> PART 1: Log the decision. Append to the end of docs/DECISIONS.md, next free number. Another session may append at the same time, so re-read the file right before appending.
> …
> PART 3: Build, test first. Write tests/unit/test_parse_body.py with synthetic inputs. Run and see them fail, then implement.
> …
> PART 4: Real-data check. Throwaway script in /tmp. Run parse_headers then parse_body on 30 random files (fixed seed)…
> Summary at the end: tests added, files changed, real-data findings, judgment calls you made. Don't commit.

*Why:* the decision is written down before the code, so the agent builds from the log and not from a guess. The "don't touch" list and "re-read before appending" make two parallel sessions safe. Seeing the tests fail first proves they test something. The real-data check found a gap that no synthetic test could: 1,079 files (7.5%) had Lotus Notes reply headers that D-019 did not define (section 3).

**2. "Don't guess the tool schema" (MCP sender, 2026-10-11 19:03).**

> Do NOT guess argument names. First write scripts/list_mcp_tools.py that connects, calls list_tools(), and prints each tool's name and input schema. Run it (listing tools sends nothing), paste the send_email schema into your plan, then build against it.
> …
> One client session per run, not one subprocess per email.
> …
> Do not run a live send yourself. Stop when tests pass and give me the exact commands for the final sequence.

*Why:* an AI will invent plausible argument names for a tool it has never seen. Reading the real schema first removed that risk. The schema showed there is no custom headers field, which shaped how the References line is sent (section 7). The last line keeps the one irreversible action, a real email, in my hands.

**3. "Audit first, then change" (parameter-driven CLI, 2026-10-11 19:23).**

> Step 1, audit only: grep the codebase for hard-coded run settings (mailbox list, maildir path, db path, output paths, report/log paths, similarity threshold, limit, recipient). List each with file and line. Then propose the CLI.
> Step 2, implement in main.py with argparse: …
> Defaults live in one place, not scattered. Library modules take these as function arguments and never read argv or env themselves.

*Why:* I wanted to see the problem before the agent changed anything. The audit found the file names hard-coded in `main.py` and a second copy of the 90.0 threshold in `dedup.py`. It also found a worse bug that I had not asked about (section 3, case 4).

**4. "Report first, then fix", with positive and negative tests (skewed timestamps, 2026-10-11 17:37).**

> 1. First, report the distribution of whole-hour gaps among the 293 (how many at 1h, 2h, ... 24h+) and confirm the rule below catches the 10-hour ones without catching 24-hour daily mail.
> 2. Log D-022 (next free number, re-read DECISIONS.md first): …
> 3. Tests first: 10h gap collapsed, 1h and 12h collapsed, 13h and 24h not collapsed, 10h gap with different minutes not collapsed, 10h gap with different main body not collapsed, 10h gap different sender not collapsed.

*Why:* step 1 makes the agent check my proposed rule against the data before building it. The tests pair every "collapsed" case with a "not collapsed" case at the boundaries (12h and 13h), so the rule can neither be too wide nor too narrow without a test failing.

**5. A prompt that checks the state first (2026-10-11 18:11).**

> First, check the state of the repo: run grep "^## D-" docs/DECISIONS.md | tail -6, git status --short, and look at src/enron_pipeline/notify.py and tests/unit/test_notify.py.
> If notify is already implemented and tested (by you or anyone), don't rebuild it. Run the full test suite and give me a summary…
> If notify is NOT implemented yet, do the following. …

*Why:* two sessions were running and I did not know which had finished notify. The prompt made the agent look first, so it would not build the same module twice. It found notify already built and committed, rebuilt nothing, and reported 716 passing tests.

## 3. Iterations and debugging

Cases 1 to 4 are in detail. Cases 5 to 8 are shorter.

### Case 1: the dedup subject regex was unanchored (caught by a test)

*What went wrong.* The agent wrote `normalize_subject` to strip `Re:`/`Fwd:` prefixes. Its regex was not anchored to the start of the string, so it also removed "re:" from the middle of a subject. "Update re: budget" became "update budget".

*How it was caught.* I had asked for subject-normalization tests, and the agent's test that only leading prefixes are stripped caught it. Of 88 dedup tests, 87 passed and `test_only_leading_prefixes_are_stripped` failed: `assert 'update budget' == 'update re: budget'`.

*Fix.* Anchor the pattern to the start. Without the test, two unrelated subjects could have shared a dedup block and the bug would have been invisible in the output.

### Case 2: 825 flagged emails, and a third of them were the same email twice (D-023)

*What went wrong.* The first dedup run flagged 825 emails in 493 groups. 293 of those were the same message in two exports, not re-sends. The Date header of one copy was shifted by whole hours (242 by exactly 10 hours), with the same minute and second and an identical main body. The quoted header text differed slightly. The stage 1 hash included the date and quoted text, so these copies never collapsed, and stage 2 flagged the later one.

*How it was caught and explained before fixing.* The agent did not just patch it. It reported the numbers, then checked one pair to see why the quoted text differed while the main body matched. It offered the fix as a what-if ("treating them as storage copies drops 493 groups to 210") and said it needed a decision from me.

*What I did.* I asked for the full gap distribution first (prompt 4 above). Of the 825 flagged emails, 300 had a whole-hour gap: 1h ×1, 4h ×6, 9h ×3, **10h ×242**, 11h ×39, 12h ×1, 24h ×1, and 7 over 24h. I then wrote the rule: same sender, same subject as written, identical main body, and a gap of 1 to 12 whole hours with identical minutes and seconds. Gaps of 13 hours or more are not collapsed, so daily automated mail is still flagged. I logged it as D-023 (my prompt said D-022, but notify had taken that number meanwhile).

*Result.* 34 new tests, 28 of them red before the change. Duplicate groups went from 493 to **222** and flagged emails from 825 to **540**. 330 rows were collapsed (281 at 10h).

*What is still wrong, and written down.* 13 skew-looking pairs are still flagged because their main bodies differ slightly (encoding artifacts). The canonical copy is the smallest `source_file`, which can be the time-shifted one. The spec ignores recipients, so 109 of the 540 flags are less than 10 minutes apart, and some of those are the same memo sent to different people.

### Case 3: a scripted edit duplicated a block in parse_body (caught by `NameError`)

*What went wrong.* For D-020, the agent changed `parse_body.py` with a scripted edit. The slice matched the wrong place and left a copy of an old helper next to the new one. The old copy still used a name that no longer existed.

*How it was caught.* After the edit, 84 of 117 parse_body tests failed with `NameError: name '_NOTES_DATE' is not defined` at `parse_body.py:359`.

*Fix.* The agent listed the functions in the file, found the duplicate block and removed exactly that block. All 117 tests passed afterwards. The tests turned a silent bad edit into an obvious failure within seconds.

### Case 4: a mailbox typo wiped the database before it was validated (found in the D-025 audit)

*What went wrong.* The first version of `run_pipeline` rebuilt the database with `init_schema` and only then started ingest. Ingest is where a missing mailbox folder raised its error. So a typo such as `--mailboxes beck-x` gave a clean error message, but only after the old database was already deleted.

*How it was caught.* Not by a test. The agent found it during the "audit only" step of the parameter-driven CLI prompt (prompt 3 above): "a typo in `--mailboxes` gave a clean error, but only after `init_schema` had wiped the previous database. My probe confirmed the old row was gone."

*Fix (D-025).* All checks run before any work: the maildir and every named mailbox must exist, the threshold must be 0 to 100, and mailbox names must be plain folder names. A failed check creates, wipes and writes nothing. The agent re-ran the same probe and the old database survived. 63 tests were added. The code had passed all of its own tests, so this is a fair example of AI code passing its tests while still having a design hole.

### Case 5: the first notify build followed the spec's letter, not its intent (D-024, D-026)

The first notify build followed the spec as written. Three things were wrong, and I found each in review or in the live test:

- **Subject.** It used the flagged email's own subject. The spec says the original's. It also gave "Re: Re: X" when the subject already started with "Re:". The agent flagged this itself in its summary, and a later audit confirmed it. Fixed in D-024 with a new `original_subject` field on `DuplicateFlag`.
- **Send log.** Both modes rewrote `output/send_log.csv` on every run. A dry run would have erased the live evidence the spec asks for. The agent listed this as a hazard in its summary. D-024 made `send_log.csv` live-only and append-only, with dry runs in `send_log_dry_run.csv`.
- **Body layout.** The spec shows a blank line between every template line, and my D-022 said to follow it "exactly". The agent reproduced the blank lines literally and listed that as a judgment call. The raw source of the first live email showed no indentation and a blank line between all 13 lines. The blank lines are an artifact of the Word document, where each line was a paragraph. D-026 changed the layout and added a golden test.

### Case 6: exploration scripts (first versions)

The first `explore_data.py` had no fixtures for body duplicates, the most important quirk. Its `examples.json` was also dominated by one mailbox (`haedicke-m`), because an alphabetical walk filled the five slots early. Both were fixed. The first three candidate mailbox sets all collapsed onto nearly the same biggest mailboxes, because the agent ranked by absolute counts. It said so and added rate-based variants. I then chose neither and used the stratified method (D-010). A folded `To:` header also polluted the shape census until the agent unfolded values first.

### Case 7: the noisy headings heuristic

The spec's "headings" field is vague. The first heuristics fired on huge numbers of files: lines ending in a colon matched 221,027 files (43%). D-019 tightened the rules. A real-data check of parse_body then found junk headings. "cc:" appeared in 720 files because Lotus Notes reply headers stayed in the body. "FYI" was a heading in 91 files. D-020 added the Notes header shapes as quoted markers and changed the all-caps rule to a word of 4 or more letters. After D-020, 1,033 of the 1,079 affected files split correctly, and files with a junk "cc:" heading went from 720 to 9. Headings remain a heuristic. About 1,710 "OMNI" tag lines from contact exports are still counted as headings, and I have not fixed that.

### Case 8: my own wrong guesses, corrected by the agent

- **`no_address@enron.com`.** I wrote this address in my exploration prompt and in D-016. The exploration found it does not exist. The real placeholders are `no.address@enron.com`, `.@enron.com` and `undisclosed-recipients`. D-016 still has my wording, because the log is append-only. The behavior is not affected, since all addresses are kept.
- **"Your empty_body detector is wrong."** I named four files that I believed were empty. The agent checked them by hand, then scanned the whole corpus two independent ways, and told me the detector was right. My own `sed` check had treated the CRLF separator line (`\r`) as empty. The corpus is 100% CRLF, and `empty_body = 0` is correct. The agent made no code change.
- **"A word of 3+ letters" in D-019.** This rule made "FYI" a heading, which I did not want. The agent implemented what I wrote, flagged the contradiction, and I amended it in D-020.

## 4. Verification

I did not trust a summary on its own. These are the checks I used.

| Check | What it showed |
|---|---|
| **Tests, written first and seen failing.** Prompts said "run and see them fail". | **846 tests pass** in 16 files (about 7,350 lines of tests for about 2,370 lines of package code). Red-to-green counts are in the transcripts, for example 88 of 88 dedup tests red before `dedup.py` existed. |
| **Mutation checks on the tests.** I had the agent break the code on purpose and confirm a test fails. | `sample_queries.sql`: 4 edits to the SQL, all caught. notify: 5 edits (pick the earliest duplicate, mark every result notified, keep microseconds, delete every file in the drafts folder, squash the blank lines), all caught. Each edit made at least one test fail, so the tests are not decorative. |
| **Real-data discovery passes before design.** | Profile of all 517,401 files, a value-shape census, frequent-line mining and a 50-file random review (`docs/exploration/`). These found folded headers, Lotus Notes shapes, LDAP-style recipients, 21,847 files with no `To`, and that Message-IDs are 100% unique, so dedup has to be content-based. |
| **Real-data checks after each parser.** | 30 random files with a fixed seed, then all 14,352 parseable files in three mailboxes, for parse_headers and parse_body. These found the Notes reply gap (case 7). |
| **Cross-checking numbers against earlier numbers.** | The full ingest gave 18,165 parsed. `SELECTION.md` had estimated 18,165. Per-mailbox counts matched exactly (11,555, 2,638, 2,134, 1,175, 663). |
| **Checking a claim against the raw export.** The README says Bcc is not real blind-copy data. | In the raw files of the five mailboxes, 5,577 of the 18,791 files have a `Cc` header. All 5,577 also have a `Bcc` header with the identical value, and no file has a `Bcc` without a `Cc`. So the export copies Cc into Bcc, and Bcc completeness (30.42%) only repeats Cc (30.42%). The first exploration run had already counted 127,853 files corpus-wide where Bcc equals Cc. The pipeline stores Bcc as it appears (D-016), and the only real blind-copy names are in `X-bcc` (19 files). The five-mailbox counts were re-checked directly on the raw files on 2026-10-13 and matched. The README's 12-mailbox sample (8,185 of 8,185) is taken from the README and was not re-run for this file. |
| **Reading flagged pairs myself.** | Three random flagged pairs side by side after each dedup change. That is how I accepted the 10-hour rule. |
| **Drafts checked by a parser.** | All 540 dry-run `.eml` files were read back with `email.parser` and compared with `duplicates_report.csv` on To, Subject and References. |
| **Live sends checked in Gmail.** | I opened the raw source of the received email twice: for the one-notice smoke test on Oct 11 and again for the final run. The agent had written that how the server renders `inReplyTo` was not verified, and asked me to look. For the final run, the Gmail search for `[Duplicate Notice]` shows the notices from the 11:29 AM run, and I opened three of them (screenshots in section 7.6). |
| **Secret scan before commits.** | The agent grepped every staged diff for my address, tokens, keys and home paths before committing. It found only test fixtures, which I then renamed to `example.com`. |

**What I did not verify.** I did not read every line of generated code, and about 60% of the repo's Python is tests. My review focused on decisions, test names and behaviors, summaries and real data. The heading heuristic, the Notes marker shapes, and the 90% similarity cutoff are judgment calls that I checked on samples, not proven correct. The final live run sent 222 notices, all to my own address, with 0 failures. I opened three in Gmail and read the raw source of one. I did not read all 222.

## 5. What I wrote vs. what AI wrote

Git cannot answer this. All 19 commits (before this one) have my git identity, because Claude Code commits through my git config. Only 7 of them carry a `Co-Authored-By: Claude` trailer.

Size of the tracked files:

| Area | Lines |
|---|---|
| Package code (`src/enron_pipeline/`) | 2,370 |
| `main.py` | 292 |
| Scripts (exploration, selection, MCP tool listing) | 2,410 |
| Unit tests | 7,352 |
| `schema.sql`, `sample_queries.sql` | 184 |
| `docs/DECISIONS.md` (26 entries) | 566 |
| `docs/plans/module-layout.md` | 485 |
| Exploration reports (generated by the scripts) | 1,415 |

My split:

| Part | Me | AI | Notes |
|---|---|---|---|
| Code, tests, SQL, scripts (about 12,600 lines) | 0% | 100% | I typed no code in the transcripts. If I edited any file by hand outside Claude Code, it would not show there. |
| Decisions D-001 to D-026 | the choices: 100% | the wording of 4 entries | I supplied the text of 22 entries in my prompts, drafted in claude.ai. The agents drafted D-020, D-024, D-025 and D-026 from my instructions. |
| Prompts and task breakdown | 100% | 0% | Drafted in the claude.ai chat, which is also AI, so "mine" means I decided and edited them. |
| `module-layout.md` | direction | text | The agents wrote it from my decisions, and later updated it themselves after each change. |
| Review, acceptance, real-data judgment | 100% | 0% | Reading summaries, eyeballing pairs, choosing thresholds. |
| Google Cloud, OAuth and MCP registration | 100% | 0% | Manual console work (section 7). The MCP client code is AI-written. |
| `README.md`, `AI_USAGE.md` | edits only | drafts | Both were drafted by Claude Code from my instructions. |

In short: the code is almost entirely AI-generated. My contribution was the decisions, the specs, the review, and the live setup.

## 6. Lessons learned

**What worked**

- **Decision log first.** Writing the decision before the code, append-only, gave every agent the same source of truth. It also made parallel sessions possible. The "next free number" rule survived two collisions.
- **Tests first, and watching them fail.** It caught the dedup regex (case 1) and the bad edit (case 3) within seconds.
- **"Report first, then fix" and "audit first".** The agents found the 10-hour skew and the DB-wipe bug because I asked them to look before changing.
- **Judgment-call lists.** Asking for "judgment calls you made" in every summary surfaced the subject mismatch, the send-log hazard and the `claude mcp` scope issue before they cost anything.
- **Real data over synthetic data.** Every parser and dedup change was checked on real files. Synthetic tests alone would have missed the Notes headers and the skewed copies.
- **Keeping the irreversible step mine.** No agent ran a live send. I ran it, then read the raw headers.
- **One task per session with `/clear`.** Each session started from the log and plan, not from stale chat.

**What was harder than expected**

- **Parallel sessions on shared files.** Both edited `DECISIONS.md` and `module-layout.md`. One agent's red tests showed up as "failures" in the other's runs. Several times an agent had to stop and work out which failures were not its own.
- **The AI builds exactly what the spec says.** The blank lines in the notice, the subject and the send-log wipe were all faithful to the text and wrong in practice. A specification is not the intent, and only a live run showed it.
- **Numbers that moved.** Flagged emails went from 825 to 540 (and groups from 493 to 222) once skewed copies were collapsed. `SELECTION.md` had estimated 68 "real resend" groups from an exact-match probe, while the fuzzy rule found 222. I had to keep asking what each number measured.
- **Heuristics never finish.** Headings and Lotus Notes shapes always have a long tail. I set cutoffs (20 files per shape) and wrote down the known gaps.
- **A lot of test code.** The tests are three times the size of the code. That is useful for catching regressions, but I cannot honestly say I have reviewed each one.
- **My own wrong claims.** I was wrong about `no_address@`, about the empty bodies, and about the "3+ letters" rule. The agents checked my claims instead of agreeing. I would keep asking them to.
- **Process I did not keep up.** The D-005 session export. I would set up the export on day one.

## 7. MCP integration

### 7.1 Which server, and why

**GongRzhe Gmail MCP server** (`@gongrzhe/server-gmail-autoauth-mcp`). I chose it because:

- it is **open source**;
- it has a **`send_email` tool**, which the assignment requires;
- it runs **locally over stdio**, so there is no hosted service and my mail never goes through a third party;
- it **handles the OAuth refresh itself**, keeping the token in `~/.gmail-mcp/credentials.json`, so a run does not need me to sign in again.

It exposes 19 tools in total (send, draft, search, read, delete, labels, filters). The pipeline calls only `send_email`.

### 7.2 Setup, step by step

1. **Google Cloud project.** Create a project at console.cloud.google.com.
2. **Enable the Gmail API.** Search for "Gmail API" and enable that one (see 7.5, issue 1).
3. **OAuth consent screen.** Choose user type **External** and leave it in **Testing**.
4. **Add yourself as a test user** on the **Audience** page (see 7.5, issue 2).
5. **Create an OAuth client.** Application type **Desktop app**. Download the JSON.
6. **Save the JSON** as `~/.gmail-mcp/gcp-oauth.keys.json`.
7. **Sign in once:** `npx @gongrzhe/server-gmail-autoauth-mcp auth`. A browser opens, you approve, and the server writes `~/.gmail-mcp/credentials.json`.
8. **Register the server with Claude Code:** `claude mcp add gmail -- npx -y @gongrzhe/server-gmail-autoauth-mcp` (see 7.5, issue 4 for scope).
9. **Restart the Claude Code session.** MCP servers only load at session start (7.5, issue 5). Check `/mcp`.
10. **For the pipeline:** `export NOTIFY_TO=you@example.com` (or pass `--notify-to`), then `python main.py --send-live --limit N`.

`mcp_config.json.example` at the repo root shows the server entry. It holds no secrets. Neither `~/.gmail-mcp/` file is ever committed. Node 18 or newer is needed for `npx`.

The server is started with `npx -y` and is **not version-pinned**, so each run uses whatever version npm serves. A pinned version would be safer.

### 7.3 How I prompted the AI tool to use `send_email`

**Manual test through Claude Code** (2026-10-11 18:58, the only prompt in its session; my address replaced with a placeholder):

> Use the gmail MCP send_email tool to send a test email to `<my Gmail address>` with subject "[Duplicate Notice] MCP test" and body "First live send from the Enron dedup pipeline setup." Show me the tool call and the result.

The agent loaded the tool schema, sent the email with exactly that subject and body, and printed the call and the result:

```
mcp__gmail__send_email
  to:      ["<my Gmail address>"]
  subject: "[Duplicate Notice] MCP test"
  body:    "First live send from the Enron dedup pipeline setup."

Email sent successfully with ID: 1a12c54c6b0940f5
```

It also said that the ID only means Gmail accepted the message, and that it had not confirmed delivery. I checked the inbox.

**Python as the MCP client** (19:03). The pipeline does not ask a model to send anything. Python calls the tool directly. The prompt (quoted in part in section 2, example 2) was:

> Python acts as an MCP client using the official `mcp` SDK (stdio transport). It launches `npx @gongrzhe/server-gmail-autoauth-mcp` and calls send_email.
> Do NOT guess argument names. First write scripts/list_mcp_tools.py that connects, calls list_tools(), and prints each tool's name and input schema. …
> One client session per run, not one subprocess per email.
> Each send returns success or failure plus an error string. Never raise into the pipeline.
> References: if send_email accepts inReplyTo or a headers-style field, pass the duplicate's message_id there. Either way keep the References line in the body as the spec template shows. Note which you did.

### 7.4 What the live sender does (D-024, D-026)

**MCP is only the transport.** The email content is deterministic. One function renders the notice from the spec template and the same text goes into the `.eml` drafts and the live email. No model writes or edits any email.

- **Schema first.** `scripts/list_mcp_tools.py` listed the tools and sent nothing. `send_email` requires `to` (an array), `subject` and `body`. It also accepts `mimeType`, `inReplyTo`, `threadId`, `cc`, `bcc`, `htmlBody` and `attachments`. It has `additionalProperties: false` and no free-form headers field.
- **What is sent.** `to = [NOTIFY_TO]`, `subject`, `body`, `mimeType = "text/plain"`, and `inReplyTo` = the flagged email's message id. The body starts with the template's four header lines (To: the real Enron sender, Subject, Date, References), then a blank line, then the notice.
- **One recipient.** Enron addresses are dead, so every live email goes to `NOTIFY_TO` (D-022). The template's To line keeps the real Enron sender.
- **One session per run.** The connection opens on the first send and closes at the end. A tool error, an MCP error or a 60-second timeout fails that one message and carries on. If the server cannot start, that message and all later ones fail with the reason, and there is no retry, so a broken setup cannot start one subprocess per email.
- **Limits.** One email per duplicate group (the latest), at most `--limit` (default 10). Live runs append to `output/send_log.csv`, so repeated live runs would resend. The final run sent 222 notices (section 7.6).
- **Tests.** 36 tests use a mocked MCP session (success, tool error, timeout, crash mid-run, server that cannot start, one shared session). No test touches the network.

### 7.5 Issues and how I resolved them

| # | Issue | Fix |
|---|---|---|
| 1 | Searching "gmail mcp" in Google Cloud showed Google's **hosted MCP APIs**, not the plain Gmail API. | The server needs the ordinary **Gmail API**. I searched for "Gmail API" and enabled that. |
| 2 | The credentials wizard did **not add me as a test user**, so sign-in was refused. | Added my account by hand on the OAuth consent screen's **Audience** page. |
| 3 | The sign-in redirects to `http://localhost:3000`. I was in a Codespace, not on my laptop. | It worked because **Codespaces forwarded port 3000** automatically, so the browser reached the server. |
| 4 | `claude mcp add` defaults to **local scope**, and in a new session the server did not show in `/mcp`. | I first registered the server with `claude mcp add`, which defaults to local scope. In a new session it didn't show in `/mcp`, so I re-added it at user scope through the `/mcp` form and restarted, and it appeared. A later `claude mcp get gmail` reported local config, so both registrations exist and the local one takes precedence. Either way the pipeline is unaffected, since the Python client launches its own server process. |
| 5 | The tool did not show up right after I registered the server. | MCP servers load **only when a session starts**. I restarted Claude Code. |
| 6 | The spec's References header cannot be set: the tool has no headers field. | Pass `inReplyTo`, and keep the References line in the body (D-024). I then checked the real headers in the live email (below). The server wrote both `In-Reply-To` and `References`. |
| 7 | The first live email had a blank line between every line and no indentation. | D-026 fixed the layout and added a golden test (case 5). |

### 7.6 Evidence

**Final run, 2026-10-13: 222 sent, 0 failed.** `output/send_log.csv` has 222 rows. Every row has status `sent` and an empty error. All 222 went to `NOTIFY_TO` (my Gmail), one per duplicate group, between 15:28:59Z and 15:29:43Z. The header, the first three rows and the last two rows, verbatim:

```csv
timestamp,recipient,subject,status,error
2026-10-13T15:28:59Z,marzookbaig14@gmail.com,[Duplicate Notice] Re: Oslo - Next Week,sent,
2026-10-13T15:29:02Z,marzookbaig14@gmail.com,[Duplicate Notice] Re: Class Confirmation - James Scribner,sent,
2026-10-13T15:29:02Z,marzookbaig14@gmail.com,[Duplicate Notice] Re: Organizational changes,sent,
...
2026-10-13T15:29:43Z,marzookbaig14@gmail.com,[Duplicate Notice] Re: Your Opinion Counts...Win $1000!,sent,
2026-10-13T15:29:43Z,marzookbaig14@gmail.com,"[Duplicate Notice] Re: Take a Survey, Win a FREE New Computer!!",sent,
```

**Raw headers of one received email from the final run** (Gmail, "Show original", notice for "IMPORTANT VIDEO ANNOUNCEMENT"):

```
Subject: [Duplicate Notice] Re: IMPORTANT VIDEO ANNOUNCEMENT
In-Reply-To: <27114403.1075862308571.JavaMail.evans@thyme>
References: <27114403.1075862308571.JavaMail.evans@thyme>
```

The message body starts with the template's header block. The real delivery address is mine, and the template's To line keeps the Enron sender:

```
To: chairman.ken@enron.com
Subject: [Duplicate Notice] Re: IMPORTANT VIDEO ANNOUNCEMENT
Date: Tue, 13 Oct 2026 15:29:38 +0000
References: <27114403.1075862308571.JavaMail.evans@thyme>
```

Both headers carry the flagged email's message id. The pipeline passes it only once, as `inReplyTo`, so the server renders that argument as both headers. That settles the "not verified" note in D-024. The same id and time appear in the pipeline's own files: `duplicates_report.csv` lists `<27114403.1075862308571.JavaMail.evans@thyme>` as the flagged email (original `<28514173.1075862151603.JavaMail.evans@thyme>`, score 100.00), and the send log has this notice at 15:29:38Z.

The first check, the one-notice smoke test on 2026-10-11 ("Oslo - Next Week"), showed the same pair of headers:

```
In-Reply-To: <8430921.1075855808059.JavaMail.evans@thyme>
References: <8430921.1075855808059.JavaMail.evans@thyme>
```

**Screenshots** (`docs/screenshots/`, all taken on 2026-10-13 after the final run):

- Gmail inbox, search `subject:"[Duplicate Notice]"`, notices received at 11:29 AM:
  [page 1](screenshots/Screenshot%202026-10-13%20at%2011.51.42%E2%80%AFAM.png),
  [scrolled](screenshots/Screenshot%202026-10-13%20at%2011.51.51%E2%80%AFAM.png),
  [scrolled to the end of the page](screenshots/Screenshot%202026-10-13%20at%2011.51.55%E2%80%AFAM.png)
- Opened notices:
  ["Your Opinion Counts...Win $1000!"](screenshots/Screenshot%202026-10-13%20at%2011.51.59%E2%80%AFAM.png),
  ["UBSW Orientation"](screenshots/Screenshot%202026-10-13%20at%2011.52.07%E2%80%AFAM.png),
  ["IMPORTANT VIDEO ANNOUNCEMENT"](screenshots/Screenshot%202026-10-13%20at%2011.52.12%E2%80%AFAM.png)
- Raw source ("Show original") of the "IMPORTANT VIDEO ANNOUNCEMENT" notice, showing `In-Reply-To` and `References`:
  [headers](screenshots/Screenshot%202026-10-13%20at%2011.58.22%E2%80%AFAM.png),
  [headers and body](screenshots/Screenshot%202026-10-13%20at%2011.58.29%E2%80%AFAM.png)
