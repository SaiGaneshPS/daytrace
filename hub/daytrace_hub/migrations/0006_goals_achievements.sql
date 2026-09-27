-- DT-53: the user's daily goals and the badges they have earned. The rules themselves (what each goal, streak
-- and badge means, and the default targets) are in data/streak_rules.json; this keeps only what the user chose
-- and what was earned. Streaks are not stored: they are worked out from the data each time.
CREATE TABLE goals (
    goal_id    TEXT PRIMARY KEY,        -- an id from streak_rules.json
    target     TEXT NOT NULL,           -- JSON: minutes as a number, a bedtime as "HH:MM"
    updated_at TEXT NOT NULL
);

-- A badge, once earned, stays earned (until the data is deleted): later data never takes it back.
CREATE TABLE achievements (
    achievement_id TEXT PRIMARY KEY,    -- an id from streak_rules.json
    earned_on      TEXT NOT NULL,       -- the local day it was earned, YYYY-MM-DD
    tz             TEXT NOT NULL,       -- the time zone that day is in
    dates          TEXT NOT NULL,       -- JSON list of the local days that counted
    unlocked_at    TEXT NOT NULL        -- when the hub first saw it earned (UTC)
);
