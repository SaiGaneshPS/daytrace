-- DT-41: a counter of changes to what the insights are worked out from, so a cached Insights tab knows when to be
-- worked out again (api/insights.py, data_version). Triggers add one on every change, whichever process makes it
-- (the hub, the tracker, a seed run), and a deleted row counts too: counting rows or reading the largest id can't
-- see a seed run that deletes its events and inserts as many again under the same ids. On devices, only what the
-- numbers show counts (name, type, paired, revoked): last_seen is written on every request and changes nothing.
CREATE TABLE data_changes (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    changes INTEGER NOT NULL
);

INSERT INTO data_changes (id, changes) VALUES (1, 0);

CREATE TRIGGER events_added AFTER INSERT ON events
BEGIN
    UPDATE data_changes SET changes = changes + 1 WHERE id = 1;
END;

CREATE TRIGGER events_replaced AFTER UPDATE ON events
BEGIN
    UPDATE data_changes SET changes = changes + 1 WHERE id = 1;
END;

CREATE TRIGGER events_deleted AFTER DELETE ON events
BEGIN
    UPDATE data_changes SET changes = changes + 1 WHERE id = 1;
END;

CREATE TRIGGER category_overrides_added AFTER INSERT ON category_overrides
BEGIN
    UPDATE data_changes SET changes = changes + 1 WHERE id = 1;
END;

CREATE TRIGGER category_overrides_changed AFTER UPDATE ON category_overrides
BEGIN
    UPDATE data_changes SET changes = changes + 1 WHERE id = 1;
END;

CREATE TRIGGER category_overrides_deleted AFTER DELETE ON category_overrides
BEGIN
    UPDATE data_changes SET changes = changes + 1 WHERE id = 1;
END;

CREATE TRIGGER devices_added AFTER INSERT ON devices
BEGIN
    UPDATE data_changes SET changes = changes + 1 WHERE id = 1;
END;

CREATE TRIGGER devices_changed AFTER UPDATE OF name, device_type, paired_at, revoked_at ON devices
BEGIN
    UPDATE data_changes SET changes = changes + 1 WHERE id = 1;
END;

CREATE TRIGGER devices_deleted AFTER DELETE ON devices
BEGIN
    UPDATE data_changes SET changes = changes + 1 WHERE id = 1;
END;
