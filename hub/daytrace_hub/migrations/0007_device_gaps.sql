-- DT-22: a device that pairs again keeps its first id. The time it was revoked in between is kept here, so the
-- stats never count a revoked stretch as days the device should have sent data.
CREATE TABLE device_gaps (
    device_id  TEXT NOT NULL REFERENCES devices (device_id),
    from_utc   TEXT NOT NULL,              -- when it was revoked
    until_utc  TEXT NOT NULL               -- when it paired again
);
CREATE INDEX device_gaps_by_device ON device_gaps (device_id);
