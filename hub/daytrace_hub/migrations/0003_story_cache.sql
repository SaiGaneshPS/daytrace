-- DT-39: the day story, cached per local day and time zone. A new story is written only when the facts behind it
-- change (facts_hash), so today's story follows new data while every other request is instant. Only stories the
-- model wrote are cached; the template used when the model is away is rebuilt every time, so the model gets
-- another chance.
CREATE TABLE story_cache (
    day         TEXT NOT NULL,          -- YYYY-MM-DD, local
    tz          TEXT NOT NULL,          -- IANA name
    facts_hash  TEXT NOT NULL,
    story       TEXT NOT NULL,
    facts       TEXT NOT NULL,          -- JSON: the facts the story was written from
    model       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (day, tz)
);
