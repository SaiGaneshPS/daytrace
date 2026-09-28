-- DT-58: the Android app's built-in dashboard gets a token of its own, handed out at pairing next to the collector
-- token: it reads the dashboard and changes settings, but never sends events. Only its hash is stored, like the
-- collector token's, and revoking the device ends both.
ALTER TABLE devices ADD COLUMN viewer_token_hash TEXT;
CREATE UNIQUE INDEX devices_by_viewer_token ON devices (viewer_token_hash) WHERE viewer_token_hash IS NOT NULL;
