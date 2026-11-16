ALTER TABLE public.ev_charging_latest_snapshot
    ADD CONSTRAINT ck_ev_charging_latest_snapshot_source
        CHECK (source = 'emapna');
ALTER TABLE public.ev_charging_latest_snapshot
    ADD CONSTRAINT ck_ev_charging_latest_snapshot_sha256
        CHECK (sha256 ~ '^[0-9a-f]{64}$');
ALTER TABLE public.ev_charging_latest_snapshot
    ADD CONSTRAINT ck_ev_charging_latest_snapshot_payload_object
        CHECK (jsonb_typeof(payload) = 'object');
ALTER TABLE public.ev_charging_latest_snapshot
    ADD CONSTRAINT ck_ev_charging_latest_snapshot_bytes_limit
        CHECK (octet_length(snapshot_bytes) <= 8000000);