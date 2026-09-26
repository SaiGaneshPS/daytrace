-- DT-13: sessions are built from the events that overlap a time window. An index on end_utc lets that query
-- skip all older history (span events), while events_by_start already serves point events.
CREATE INDEX events_by_end ON events (end_utc);
