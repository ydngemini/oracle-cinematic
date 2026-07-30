-- 0033_authorized_pdf_source_registry.sql
--
-- Global registry for verified government PDFs. These are links to source
-- documents, not copied form bodies. A row is exposed only when it has been
-- manually verified as a direct HTTPS PDF from the named authority.

CREATE TABLE IF NOT EXISTS authorized_document_sources (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_key          text NOT NULL UNIQUE,
    authority_scope     text NOT NULL CHECK (authority_scope IN ('federal', 'state')),
    state_code          char(2) REFERENCES state_regulatory_profiles(state_code),
    document_kind       text NOT NULL CHECK (document_kind IN ('contract', 'document')),
    title               text NOT NULL,
    subtitle            text NOT NULL DEFAULT '',
    source_name         text NOT NULL,
    source_url          text NOT NULL CHECK (source_url LIKE 'https://%'),
    pdf_url             text NOT NULL CHECK (pdf_url LIKE 'https://%'),
    media_type          text NOT NULL DEFAULT 'application/pdf'
                            CHECK (media_type = 'application/pdf'),
    version             text,
    effective_date      date,
    approval_status     text NOT NULL DEFAULT 'approved'
                            CHECK (approval_status IN ('approved', 'retired')),
    verified_at         timestamptz NOT NULL DEFAULT now(),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (authority_scope = 'federal' AND state_code IS NULL)
        OR (authority_scope = 'state' AND state_code IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_authorized_document_sources_active
    ON authorized_document_sources (authority_scope, state_code, document_kind)
    WHERE approval_status = 'approved';

-- All source pages and PDFs below were verified directly from the named
-- government authority. The NY DOS PDF endpoints intentionally have no .pdf
-- suffix; their verified media type is stored in the registry rather than
-- inferred from a filename.
INSERT INTO authorized_document_sources (
    source_key, authority_scope, state_code, document_kind, title, subtitle,
    source_name, source_url, pdf_url, version, effective_date, approval_status
) VALUES
    (
        'epa-lead-seller-disclosure-en', 'federal', NULL, 'document',
        'Lead-Based Paint Seller Disclosure', 'US federal · Form 9600-040 · English',
        'U.S. Environmental Protection Agency',
        'https://www.epa.gov/lead/lead-based-paint-disclosure-rule-section-1018-title-x',
        'https://www.epa.gov/sites/default/files/documents/selr_eng.pdf',
        '9600-040', NULL, 'approved'
    ),
    (
        'epa-lead-lessor-disclosure-en', 'federal', NULL, 'document',
        'Lead-Based Paint Lessor Disclosure', 'US federal · Form 9600-041 · English',
        'U.S. Environmental Protection Agency',
        'https://www.epa.gov/lead/lead-based-paint-disclosure-rule-section-1018-title-x',
        'https://www.epa.gov/sites/default/files/documents/lesr_eng.pdf',
        '9600-041', NULL, 'approved'
    ),
    (
        'tx-trec-20-19-one-to-four-resale', 'state', 'TX', 'contract',
        'One to Four Family Residential Contract (Resale)', 'TX · TREC 20-19 · residential resale',
        'Texas Real Estate Commission',
        'https://www.trec.texas.gov/forms/one-four-family-residential-contract-resale-0',
        'https://www.trec.texas.gov/sites/default/files/pdf-forms/20-19_2.pdf',
        '20-19', DATE '2026-07-01', 'approved'
    ),
    (
        'ny-dos-property-condition-disclosure-2025', 'state', 'NY', 'document',
        'Property Condition Disclosure Statement', 'NY · required beginning 2025-07-01',
        'New York Department of State',
        'https://dos.ny.gov/additional-forms-real-estate-salesperson',
        'https://dos.ny.gov/property-condition-disclosure-statement-eff-7125',
        'DOS-1614-f', DATE '2025-07-01', 'approved'
    ),
    (
        'ny-dos-buyer-seller-disclosure-en', 'state', 'NY', 'document',
        'Buyer and Seller Disclosure Form', 'NY · English',
        'New York Department of State',
        'https://dos.ny.gov/additional-forms-real-estate-salesperson',
        'https://dos.ny.gov/buyer-and-seller-disclosure-form-english',
        NULL, NULL, 'approved'
    ),
    (
        'pa-srec-seller-property-disclosure', 'state', 'PA', 'document',
        'Seller''s Property Disclosure Statement', 'PA · seller disclosure',
        'Pennsylvania State Real Estate Commission',
        'https://www.pa.gov/agencies/dos/department-and-offices/bpoa/boards-commissions/real-estate-commission',
        'https://www.pa.gov/content/dam/copapwp-pagov/en/dos/department-and-offices/bpoa/real-estate/Sellers%20Property%20Disclosure%20Statement.pdf',
        NULL, NULL, 'approved'
    ),
    (
        'pa-srec-written-consumer-notice', 'state', 'PA', 'document',
        'Written Consumer Notice', 'PA · consumer notice',
        'Pennsylvania State Real Estate Commission',
        'https://www.pa.gov/agencies/dos/department-and-offices/bpoa/boards-commissions/real-estate-commission',
        'https://www.pa.gov/content/dam/copapwp-pagov/en/dos/department-and-offices/bpoa/real-estate/Written%20Consumer%20Notice.pdf',
        NULL, NULL, 'approved'
    )
ON CONFLICT (source_key) DO UPDATE
SET authority_scope = EXCLUDED.authority_scope,
    state_code = EXCLUDED.state_code,
    document_kind = EXCLUDED.document_kind,
    title = EXCLUDED.title,
    subtitle = EXCLUDED.subtitle,
    source_name = EXCLUDED.source_name,
    source_url = EXCLUDED.source_url,
    pdf_url = EXCLUDED.pdf_url,
    media_type = 'application/pdf',
    version = EXCLUDED.version,
    effective_date = EXCLUDED.effective_date,
    approval_status = EXCLUDED.approval_status,
    verified_at = now(),
    updated_at = now();

ALTER TABLE authorized_document_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE authorized_document_sources FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS authorized_document_sources_authenticated_read
    ON authorized_document_sources;
CREATE POLICY authorized_document_sources_authenticated_read
    ON authorized_document_sources
    FOR SELECT
    USING (app_current_tenant() IS NOT NULL OR app_is_platform_admin());

GRANT SELECT ON authorized_document_sources TO oracle_app;
