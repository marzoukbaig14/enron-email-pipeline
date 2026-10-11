-- Enron email pipeline schema (SQLite). See docs/DECISIONS.md D-012.
--
-- Foreign keys are only enforced if the connection runs
-- PRAGMA foreign_keys = ON; SQLite leaves it OFF by default.

-- emails: one row per source file, storage copies included. Copies are
-- grouped, not merged: copy_of points at the canonical copy's message_id
-- (NULL on the canonical row) and content_hash is used to find copies.
-- is_duplicate / duplicate_of / similarity_score hold the Task 3 fuzzy
-- duplicate result; notification_* tracks the Task 4 notice.
-- Dates are ISO 8601 UTC text; the original header is kept in date_raw.
CREATE TABLE emails (
    id                 INTEGER PRIMARY KEY,
    message_id         TEXT    NOT NULL UNIQUE,
    date               TEXT    NOT NULL,            -- ISO 8601 UTC
    date_raw           TEXT,
    from_address       TEXT    NOT NULL,
    subject            TEXT    NOT NULL,            -- empty string allowed
    body               TEXT    NOT NULL,            -- empty string allowed
    source_file        TEXT    NOT NULL UNIQUE,
    mailbox            TEXT    NOT NULL,
    x_from             TEXT,
    x_to               TEXT,
    x_cc               TEXT,
    x_bcc              TEXT,
    x_folder           TEXT,
    x_origin           TEXT,
    content_type       TEXT,
    has_attachment     INTEGER NOT NULL DEFAULT 0 CHECK (has_attachment IN (0, 1)),
    forwarded_content  TEXT,
    quoted_content     TEXT,
    headings           TEXT,
    content_hash       TEXT,
    copy_of            TEXT    REFERENCES emails(message_id),
    is_duplicate       INTEGER NOT NULL DEFAULT 0 CHECK (is_duplicate IN (0, 1)),
    duplicate_of       TEXT    REFERENCES emails(message_id),
    similarity_score   REAL,
    notification_sent  INTEGER NOT NULL DEFAULT 0 CHECK (notification_sent IN (0, 1)),
    notification_date  TEXT
);

-- recipients: normalized To/Cc/Bcc addresses, one row per address. position
-- keeps the order within its header. At larger scale the addresses would
-- move to their own table and be referenced by id.
CREATE TABLE recipients (
    id        INTEGER PRIMARY KEY,
    email_id  INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    address   TEXT    NOT NULL,
    kind      TEXT    NOT NULL CHECK (kind IN ('to', 'cc', 'bcc')),
    position  INTEGER NOT NULL,                     -- order within that header
    UNIQUE (email_id, kind, position)
);

-- parse_failures: files that could not be parsed, with the reason. Mirrors
-- error_log.txt so failures can be counted and grouped with SQL.
CREATE TABLE parse_failures (
    id           INTEGER PRIMARY KEY,
    source_file  TEXT NOT NULL,
    reason       TEXT NOT NULL,
    failed_at    TEXT NOT NULL                      -- ISO 8601 UTC
);

CREATE INDEX idx_emails_date          ON emails(date);
CREATE INDEX idx_emails_from_address  ON emails(from_address);
CREATE INDEX idx_emails_subject       ON emails(subject);
CREATE INDEX idx_emails_mailbox       ON emails(mailbox);
CREATE INDEX idx_emails_content_hash  ON emails(content_hash);
CREATE INDEX idx_emails_copy_of       ON emails(copy_of);
CREATE INDEX idx_emails_duplicate_of  ON emails(duplicate_of);
CREATE INDEX idx_recipients_email_id  ON recipients(email_id);
CREATE INDEX idx_recipients_address   ON recipients(address);
