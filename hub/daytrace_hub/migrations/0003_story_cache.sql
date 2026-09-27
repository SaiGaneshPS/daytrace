-- DT-39: the day story, cached per local day and time zone. A cached story is served while the facts behind it
-- (facts_hash) and what wrote it (writer: the prompt and number check, the configured model, and whether the day
-- was over) stay the same; today's story is also served while it is under 15 minutes old. Only stories the model
-- wrote are cached; the template used when the model is away is rebuilt every time, so the model gets another
-- chance.
CREATE TABLE story_cache (
    day         TEXT NOT NULL,          -- YYYY-MM-DD, local
    tz          TEXT NOT NULL,          -- IANA name
    facts_hash  TEXT NOT NULL,
    writer      TEXT NOT NULL,
    story       TEXT NOT NULL,
    facts       TEXT NOT NULL,          -- JSON: the facts the story was written from (served with it)
    model       TEXT NOT NULL,
    created_at  TEXT NOT NULL,          -- when those facts were read; an older story never replaces a newer one
    PRIMARY KEY (day, tz)
);
