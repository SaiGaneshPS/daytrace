-- DT-22: a phone that pairs again gets its first id back, instead of a new one each time.
-- device_key_hash is the SHA-256 of a key only that phone's Daytrace app can make (it survives a reinstall); NULL for
-- a device that never sent one (the desktop tracker, the demo data, Shortcuts, browsers).
ALTER TABLE devices ADD COLUMN device_key_hash TEXT;
CREATE UNIQUE INDEX devices_by_key ON devices (device_key_hash) WHERE device_key_hash IS NOT NULL;
