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


-- 6. Flagged duplicates next to their originals
-- Shows: every email that dedup flagged as a duplicate (is_duplicate = 1),
--   joined to the original it repeats (duplicate_of), with both dates and the
--   similarity score, most similar first (D-021). These are the same rows and
--   columns as duplicates_report.csv, in a different order: the CSV is sorted
--   by original, this one by similarity. The join uses the unique index on
--   message_id. The score is shown with 2 decimals but sorted unrounded.
-- Expected output: one row per flagged email. similarity_score lies between
--   the threshold (90 by default) and 100 and never increases down the list;
--   ties are ordered by the duplicate's date. original_date is never later
--   than duplicate_date. Storage copies (copy_of is set) and emails that were
--   not flagged do not appear. Zero rows is valid if nothing was flagged.
-- Real result (D-013 mailboxes): 540 rows from 222 distinct originals, the
--   same as the 222 duplicate groups. 66 rows score exactly 100. The first is
--   "Organizational Changes" from office.chairman@enron.com, flagged at
--   2000-05-08T06:21:00Z against an original sent 2000-05-05T17:33:00Z. The
--   last is "Vermont Yankee Notification" from bobbi.kilburn@vynpc.com, with
--   a score of 90.15.
-- Columns: duplicate_message_id, original_message_id, subject, from_address, duplicate_date, original_date, similarity_score
SELECT d.message_id AS duplicate_message_id,
       o.message_id AS original_message_id,
       d.subject,
       d.from_address,
       d.date AS duplicate_date,
       o.date AS original_date,
       ROUND(d.similarity_score, 2) AS similarity_score
FROM emails AS d
JOIN emails AS o
  ON o.message_id = d.duplicate_of
WHERE d.is_duplicate = 1
ORDER BY d.similarity_score DESC, d.date, d.id;
