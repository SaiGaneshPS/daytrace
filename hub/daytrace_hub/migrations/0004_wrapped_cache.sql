-- DT-41: the week's Wrapped lines, cached per week (its Monday) and time zone, like the day story (0003): served
-- while the facts behind them (facts_hash) and what wrote them (writer) stay the same; a week that is not over is
-- also served while under 15 minutes old. Only lines the model wrote are cached.
CREATE TABLE wrapped_cache (
    week        TEXT NOT NULL,          -- the week's Monday, YYYY-MM-DD, local
    tz          TEXT NOT NULL,          -- IANA name
    facts_hash  TEXT NOT NULL,
    writer      TEXT NOT NULL,
    lines       TEXT NOT NULL,          -- JSON list of the lines
    facts       TEXT NOT NULL,          -- JSON: the facts they were written from (served with them)
    model       TEXT NOT NULL,
    created_at  TEXT NOT NULL,          -- when those facts were read; older lines never replace newer ones
    PRIMARY KEY (week, tz)
);
