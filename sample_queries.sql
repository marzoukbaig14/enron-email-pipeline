-- Sample queries for the Enron email database (schema.sql, SQLite).
--
-- Run one against a pipeline database, for example:
--     sqlite3 output/enron.db < sample_queries.sql
-- Each query is a single statement that runs as written, with no parameters.
-- The "Columns" line under each query names its result columns in order.
-- tests/unit/test_sample_queries.py checks that line against the real result.
--
-- Dates are stored as ISO 8601 UTC text ending in "Z" (for example
-- 2001-05-14T23:39:00Z), so plain string comparison orders them correctly.


-- 1. Emails per sender (top 10)
-- Shows: which addresses sent the most emails in the stored set.
-- Expected output: at most 10 rows, one per sender, busiest first (ties
--   broken by address). email_count is a positive integer and the counts
--   never increase down the list.
-- Real result (D-013 mailboxes, 18,165 emails): 10 rows. The top three are
--   sally.beck@enron.com 4225, m..presto@enron.com 785 and
--   andrea.ring@enron.com 462; the 10th is mike.jordan@enron.com 212.
-- Columns: from_address, email_count
SELECT from_address,
       COUNT(*) AS email_count
FROM emails
GROUP BY from_address
ORDER BY email_count DESC, from_address
LIMIT 10;


-- 2. Emails in a date range
-- Shows: every email sent during May 2001 (UTC), oldest first. The range
--   includes its start and excludes its end, so adjacent months never overlap.
--   Change the two dates to look at another period; idx_emails_date makes
--   this a range scan, not a full table scan.
-- Expected output: one row per matching email. Every date lies between
--   2001-05-01T00:00:00Z and 2001-05-31T23:59:59Z. Zero rows is valid if the
--   selected mailboxes have nothing in that month.
-- Real result (D-013 mailboxes): 1,041 rows. The first is
--   2001-05-01T00:10:00Z, paul.y'barbo@enron.com, "US Natural Gas Prices".
-- Columns: date, from_address, subject
SELECT date,
       from_address,
       subject
FROM emails
WHERE date >= '2001-05-01T00:00:00Z'
  AND date <  '2001-06-01T00:00:00Z'
ORDER BY date, id;


-- 3. Emails with CC recipients
-- Shows: emails that were copied to at least one person, with how many, and
--   the most heavily copied first. This is the join between emails and the
--   normalized recipients table (kind = 'cc').
-- Expected output: one row per email that has one or more CC recipients,
--   with cc_count >= 1 and counts never increasing down the list. Emails
--   with no CC recipients do not appear. (Bcc mirrors Cc in this corpus, so
--   it is not counted here.)
-- Real result (D-013 mailboxes): 5,526 rows. The first is "Revised LNG Team
--   List" from kathleen.hardeman@enron.com with cc_count 81, followed by
--   several kathie.grabstald@enron.com "News Deadline" mailings with 54-56.
-- Columns: message_id, from_address, subject, cc_count
SELECT e.message_id,
       e.from_address,
       e.subject,
       COUNT(r.id) AS cc_count
FROM emails AS e
JOIN recipients AS r
  ON r.email_id = e.id
 AND r.kind = 'cc'
GROUP BY e.id
ORDER BY cc_count DESC, e.date, e.id;


-- 4. Parse failures by reason
-- Shows: why files were rejected, most common reason first. Same numbers as
--   "Failures by reason" in output/extraction_stats.json, but from the table.
-- Expected output: one row per distinct reason, such as "missing to", with
--   its count. The counts add up to the number of lines in error_log.txt.
--   Zero rows means nothing failed.
-- Real result (D-013 mailboxes): 1 row, "missing to" with 626, the same as
--   the 626 lines of error_log.txt.
-- Columns: reason, failure_count
SELECT reason,
       COUNT(*) AS failure_count
FROM parse_failures
GROUP BY reason
ORDER BY failure_count DESC, reason;


-- 5. Emails per mailbox
-- Shows: how the stored emails split across the selected mailboxes (D-013),
--   largest first.
-- Expected output: one row per ingested mailbox (at most 5 with the default
--   set). email_count is a positive integer, and the counts add up to the
--   total number of rows in emails.
-- Real result (D-013 mailboxes): 5 rows adding up to 18,165: beck-s 11555,
--   baughman-d 2638, presto-k 2134, ybarbo-p 1175, ring-a 663.
-- Columns: mailbox, email_count
SELECT mailbox,
       COUNT(*) AS email_count
FROM emails
GROUP BY mailbox
ORDER BY email_count DESC, mailbox;


-- 6. Duplicate queries (placeholder)
-- TODO: add after dedup is built. Planned, using the is_duplicate,
--   duplicate_of and similarity_score columns:
--     - flagged duplicates next to their originals, with similarity score;
--     - number of emails flagged per sender;
--     - storage copies (copy_of is not NULL) grouped by original.
--   Each will follow the format above: a Shows line, an Expected output
--   line and a Columns line.
