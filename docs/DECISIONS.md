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
