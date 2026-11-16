-- Latest-only EV charging snapshot (single row source='emapna').
-- snapshot_bytes holds exact canonical JSON bytes used for SHA-256 verification.

CREATE TABLE IF NOT EXISTS public.ev_charging_latest_snapshot (
    source          VARCHAR(32) PRIMARY KEY,
    sha256          VARCHAR(64) NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload         JSONB NOT NULL,
    snapshot_bytes  BYTEA NOT NULL,
    CONSTRAINT ck_ev_charging_latest_snapshot_source
        CHECK (source = 'emapna'),
    CONSTRAINT ck_ev_charging_latest_snapshot_sha256
        CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_ev_charging_latest_snapshot_payload_object
        CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT ck_ev_charging_latest_snapshot_bytes_limit
        CHECK (octet_length(snapshot_bytes) <= 8000000)
);

COMMENT ON TABLE public.ev_charging_latest_snapshot IS
    'Singleton latest complete eMapna EV charging snapshot without history rows';
COMMENT ON COLUMN public.ev_charging_latest_snapshot.snapshot_bytes IS
    'Canonical JSON bytes with SHA-256 matching sha256 column';