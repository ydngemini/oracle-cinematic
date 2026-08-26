-- A plan derived from a reconstruction could never be saved.
--
-- `floorplan_pipeline.schema` has carried "reconstruction" as a Provenance
-- source since the splat-slicing path was built, deliberately kept distinct
-- from "ai_vision": one is measured 3D structure sliced horizontally, the other
-- is a model's guess from flat photos, and collapsing them puts invented and
-- measured geometry under one word on a surface that feeds rehab costing.
--
-- Neither the API validator nor this CHECK was updated to match, so the
-- pipeline produced documents that both layers rejected. Nothing surfaced it
-- until a reconstruction actually tried to write one, because until now nothing
-- did — the worker only started deriving plans in this same branch.
--
-- Idempotent: the constraint is dropped by name and re-added, so re-running is
-- safe and a database already carrying the wider list is left as it is.

ALTER TABLE property_floorplans
    DROP CONSTRAINT IF EXISTS property_floorplans_source_chk;

ALTER TABLE property_floorplans
    ADD CONSTRAINT property_floorplans_source_chk CHECK (
        source IN ('manual', 'ai_vision', 'parcel_vector', 'imported', 'reconstruction')
    );

-- The revisions table stores the document as jsonb and does not constrain
-- source, so it needs no change: a revision is a copy of whatever the parent
-- row was allowed to hold.
