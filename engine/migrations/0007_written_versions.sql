-- The version whose content was last written to the backend. It differs from `version` when a
-- new version with identical content was confirmed without rewriting, which keeps the provider's
-- metadata on the earlier version. Cross-checks compare against this value.
ALTER TABLE record_locations ADD COLUMN written_version TEXT;
UPDATE record_locations SET written_version = version WHERE written_version IS NULL;
