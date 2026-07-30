-- 0035_expanded_authoritative_form_sources.sql
--
-- Broaden the contract/document directory without importing protected form
-- bodies.  The catalogue now distinguishes federal sources from state sources,
-- keeps direct downloads limited to reviewed government PDFs, and presents
-- association-controlled forms as outbound provider links only.

ALTER TABLE authorized_form_source_links
    ADD COLUMN IF NOT EXISTS authority_scope text NOT NULL DEFAULT 'state';

ALTER TABLE authorized_form_source_links
    ALTER COLUMN state_code DROP NOT NULL;

ALTER TABLE authorized_form_source_links
    DROP CONSTRAINT IF EXISTS authorized_form_source_links_authority_scope_check;
ALTER TABLE authorized_form_source_links
    ADD CONSTRAINT authorized_form_source_links_authority_scope_check
    CHECK (authority_scope IN ('federal', 'state'));

ALTER TABLE authorized_form_source_links
    DROP CONSTRAINT IF EXISTS authorized_form_source_links_scope_state_check;
ALTER TABLE authorized_form_source_links
    ADD CONSTRAINT authorized_form_source_links_scope_state_check
    CHECK (
        (authority_scope = 'federal' AND state_code IS NULL)
        OR (authority_scope = 'state' AND state_code IS NOT NULL)
    );

CREATE INDEX IF NOT EXISTS idx_authorized_form_source_links_scope_active
    ON authorized_form_source_links (authority_scope, state_code, document_kind)
    WHERE approval_status = 'approved';

-- Each state now has an official regulator or government source in addition
-- to any existing association library.  These links are source records, not
-- an assertion that the regulator publishes every private transaction form.
INSERT INTO authorized_form_source_links (
    source_key, authority_scope, state_code, document_kind, title, subtitle,
    source_name, source_url, access_mode, access_note, approval_status
) VALUES
    ('al-arec-forms', 'state', 'AL', 'document', 'Alabama Real Estate Commission forms', 'AL · official regulator forms', 'Alabama Real Estate Commission', 'https://arec.alabama.gov/pages/forms.aspx', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('ar-arec-forms', 'state', 'AR', 'document', 'Arkansas Real Estate Commission forms', 'AR · official regulator forms', 'Arkansas Real Estate Commission', 'https://arec.arkansas.gov/forms-publications/forms/', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('az-adre-forms', 'state', 'AZ', 'document', 'Arizona Department of Real Estate forms', 'AZ · official regulator forms', 'Arizona Department of Real Estate', 'https://azre.gov/all-adre-resources/adre-forms', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('ca-dre-forms', 'state', 'CA', 'document', 'California Department of Real Estate forms', 'CA · official regulator forms', 'California Department of Real Estate', 'https://www.dre.ca.gov/forms/licensing.html', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('ct-dcp-new-real-estate-forms', 'state', 'CT', 'document', 'Connecticut real estate disclosure forms', 'CT · official regulatory forms', 'Connecticut Department of Consumer Protection', 'https://portal.ct.gov/dcp/real-estate/new-forms', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('de-dpr-real-estate-forms', 'state', 'DE', 'document', 'Delaware Real Estate Commission forms', 'DE · official regulator forms', 'Delaware Division of Professional Regulation', 'https://dpr.delaware.gov/boards/realestate/forms/', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('fl-frec-forms-publications', 'state', 'FL', 'document', 'Florida Real Estate Commission forms and publications', 'FL · official regulator forms', 'Florida Real Estate Commission', 'https://www2.myfloridalicense.com/real-estate-commission/forms-and-publications/', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('ga-grec-official-portal', 'state', 'GA', 'document', 'Georgia Real Estate Commission official portal', 'GA · regulatory information and forms', 'Georgia Real Estate Commission & Appraisers Board', 'https://grec.state.ga.us/', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('hi-rec-forms', 'state', 'HI', 'document', 'Hawaii Real Estate Commission forms', 'HI · official regulator forms', 'Hawaii Real Estate Commission', 'https://cca.hawaii.gov/reb/rec_forms/', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('id-rec-official-portal', 'state', 'ID', 'document', 'Idaho Real Estate Commission forms', 'ID · official regulator forms', 'Idaho Division of Occupational and Professional Licenses', 'https://dopl.idaho.gov/rec/', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('il-dfpr-real-estate-forms', 'state', 'IL', 'document', 'Illinois Division of Real Estate forms', 'IL · official regulator forms', 'Illinois Department of Financial and Professional Regulation', 'https://idfprapps.illinois.gov/Forms/DRE/APPFORMS.asp', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('in-pla-real-estate-licensing', 'state', 'IN', 'document', 'Indiana real estate licensing forms', 'IN · official regulator resources', 'Indiana Professional Licensing Agency', 'https://www.in.gov/pla/professions/real-estate-home/real-estate-licensing-information/', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('ia-real-estate-commission', 'state', 'IA', 'document', 'Iowa Real Estate Commission portal', 'IA · official regulator forms and resources', 'Iowa Department of Inspections, Appeals, and Licensing', 'https://dial.iowa.gov/about-dial/boards/real-estate', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('ks-krec-compliance', 'state', 'KS', 'document', 'Kansas Real Estate Commission compliance forms', 'KS · official compliance forms', 'Kansas Real Estate Commission', 'https://www.krec.ks.gov/compliance', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('me-rec-applications-forms', 'state', 'ME', 'document', 'Maine Real Estate Commission applications and forms', 'ME · official regulator forms', 'Maine Office of Professional and Occupational Regulation', 'https://www.maine.gov/pfr/professionallicensing/professions/real-estate-commission/applications-forms', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('md-mrec-forms', 'state', 'MD', 'document', 'Maryland Real Estate Commission forms', 'MD · official disclosures and forms', 'Maryland Department of Labor', 'https://labor.maryland.gov/license/mrec/', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('ma-board-real-estate-forms', 'state', 'MA', 'document', 'Massachusetts real estate broker and salesperson forms', 'MA · official regulator forms', 'Massachusetts Board of Registration of Real Estate Brokers and Salespersons', 'https://www.mass.gov/lists/forms-for-real-estate-brokers-and-salespersons', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('mi-lara-real-estate-forms', 'state', 'MI', 'document', 'Michigan real estate broker and salesperson forms', 'MI · official regulator forms', 'Michigan Department of Licensing and Regulatory Affairs', 'https://www.michigan.gov/lara/bureau-list/bpl/occ/prof/real-estate', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('mn-commerce-real-estate-forms', 'state', 'MN', 'document', 'Minnesota real estate conveyancing forms', 'MN · official conveyancing forms', 'Minnesota Department of Commerce', 'https://mn.gov/commerce/business/real-estate/forms.jsp', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('mo-real-estate-commission-forms', 'state', 'MO', 'document', 'Missouri Real Estate Commission forms', 'MO · official regulator forms', 'Missouri Real Estate Commission', 'https://pr.mo.gov/realestate-application-forms.asp', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('mt-board-realty-regulation-forms', 'state', 'MT', 'document', 'Montana Board of Realty Regulation forms', 'MT · official regulator forms', 'Montana Board of Realty Regulation', 'https://boards.bsd.dli.mt.gov/realty-regulation/forms', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('nh-state-forms-finder', 'state', 'NH', 'document', 'New Hampshire real estate forms finder', 'NH · official state forms', 'State of New Hampshire', 'https://onlineforms.nh.gov/nform/finder', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('nj-rec-applications-forms', 'state', 'NJ', 'document', 'New Jersey Real Estate Commission applications and forms', 'NJ · official regulator forms', 'New Jersey Department of Banking and Insurance', 'https://www.nj.gov/dobi/division_rec/licensing/recforms.htm', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('nm-real-estate-commission', 'state', 'NM', 'document', 'New Mexico Real Estate Commission portal', 'NM · official regulator forms and guidance', 'New Mexico Regulation and Licensing Department', 'https://www.rld.nm.gov/boards-and-commissions/individual-boards-and-commissions/real-estate-commission/', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('nc-real-estate-commission', 'state', 'NC', 'document', 'North Carolina Real Estate Commission portal', 'NC · official disclosures and regulatory resources', 'North Carolina Real Estate Commission', 'https://www.ncrec.gov/', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('nd-state-forms', 'state', 'ND', 'document', 'North Dakota public forms portal', 'ND · official public forms', 'North Dakota State Government', 'https://www.nd.gov/eforms/', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('oh-real-estate-professional-licensing', 'state', 'OH', 'document', 'Ohio Division of Real Estate and Professional Licensing', 'OH · official regulator forms and licensing', 'Ohio Department of Commerce', 'https://com.ohio.gov/divisions-and-programs/real-estate-and-professional-licensing', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('or-rea-initial-agency-disclosure-portal', 'state', 'OR', 'document', 'Oregon Real Estate Agency disclosure resources', 'OR · official agency disclosure forms', 'Oregon Real Estate Agency', 'https://www.oregon.gov/rea/licensing/Pages/Initial-Agency-Disclosure.aspx', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('pa-real-estate-commission-resources', 'state', 'PA', 'document', 'Pennsylvania Real Estate Commission resources', 'PA · official regulator forms and resources', 'Pennsylvania Department of State', 'https://www.pa.gov/agencies/dos/department-and-offices/bpoa/boards-commissions/real-estate-commission/resources-and-documents', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('ri-dbr-real-estate', 'state', 'RI', 'document', 'Rhode Island real estate applications and forms', 'RI · official regulator forms', 'Rhode Island Department of Business Regulation', 'https://dbr.ri.gov/real-estate-and-commercial-licensing/real-estate', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('tn-real-estate-commission', 'state', 'TN', 'document', 'Tennessee Real Estate Commission forms and downloads', 'TN · official regulator forms', 'Tennessee Real Estate Commission', 'https://www.tn.gov/commerce/regboards/trec.html', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('va-real-estate-board-disclosures', 'state', 'VA', 'document', 'Virginia Real Estate Board disclosure forms', 'VA · official transaction disclosures', 'Virginia Department of Professional and Occupational Regulation', 'https://prd-dpor.virginiainteractive.org/Consumers/Disclosure_Forms', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),
    ('vt-real-estate-commission-disclosure', 'state', 'VT', 'document', 'Vermont Real Estate Commission consumer disclosure', 'VT · mandatory consumer disclosure', 'Vermont Office of Professional Regulation', 'https://sos.vermont.gov/media/jymjgau5/mandatory-consumer-disclosure-for-a-designated-agency-brokerage-firm-9-24-2015.pdf', 'public_portal', 'Official government source. The source opens at the issuing authority and is not rehosted by NEOH.', 'approved'),
    ('wa-dol-real-estate-forms', 'state', 'WA', 'document', 'Washington real estate broker forms', 'WA · official consumer and broker forms', 'Washington State Department of Licensing', 'https://dol.wa.gov/professional-licenses/real-estate-brokers/forms-real-estate-brokers', 'public_portal', 'Official government portal. Direct public PDFs are listed separately when available.', 'approved'),

    -- Confirmed association/provider sources for states whose official source
    -- is already public.  These continue to be outbound links only.
    ('co-realtors-forms-contract-software', 'state', 'CO', 'contract', 'Colorado REALTORS® forms and contract software', 'CO · association transaction forms', 'Colorado REALTORS®', 'https://coloradorealtors.com/forms-contract-software/', 'licensed_association', 'Provider access is governed by Colorado REALTORS® and its form vendors. NEOH opens the source; it does not copy or rehost the form text.', 'approved'),
    ('ky-realtors-residential-sales-contract', 'state', 'KY', 'contract', 'Kentucky REALTORS® Residential Sales Contract', 'KY · association residential sales contract', 'Kentucky REALTORS®', 'https://kyrealtors.com/wp-content/uploads/2025/06/KYR-Residential-Sales-Contract-2025.pdf', 'licensed_association', 'The source form identifies member-only licensed use. NEOH opens the source; it does not copy or rehost the form text.', 'approved'),
    ('la-realtors-mandatory-form-updates', 'state', 'LA', 'document', 'Louisiana REALTORS® mandatory form updates', 'LA · association guidance for commission forms', 'Louisiana REALTORS®', 'https://www.larealtors.org/2026-changes-to-purchase-agreement-and-property-disclosure', 'licensed_association', 'Association guidance and forms may be governed by provider terms. NEOH opens the source; it does not copy or rehost protected text.', 'approved'),
    ('ms-realtors-standard-forms', 'state', 'MS', 'contract', 'Mississippi REALTORS® standard forms', 'MS · member contract and disclosure library', 'Mississippi REALTORS®', 'https://msrealtors.org/mar-standard-forms/', 'licensed_association', 'Member login is required for the provider library. NEOH opens the source; it does not copy or rehost the form text.', 'approved'),
    ('ne-realtors-form-access', 'state', 'NE', 'contract', 'Nebraska REALTORS® form access', 'NE · member/vendor form library', 'Nebraska REALTORS® Association', 'https://nebraskarealtors.com/how-to-get-started-with-forms/', 'licensed_association', 'Provider access is governed by the association and form vendor terms. NEOH opens the source; it does not copy or rehost the form text.', 'approved'),
    ('ny-nysar-statewide-forms', 'state', 'NY', 'contract', 'NYSAR statewide forms resources', 'NY · association transaction forms', 'New York State Association of REALTORS®', 'https://www.nysar.com/legal/', 'licensed_association', 'Association access and use are governed by NYSAR membership or provider terms. NEOH opens the source; it does not copy or rehost the form text.', 'approved'),
    ('sc-realtors-forms', 'state', 'SC', 'contract', 'South Carolina REALTORS® forms library', 'SC · member forms and contracts', 'South Carolina REALTORS®', 'https://screaltors.org/forms/', 'licensed_association', 'Member login is required for the provider library. NEOH opens the source; it does not copy or rehost the form text.', 'approved'),
    ('tx-realtors-forms', 'state', 'TX', 'contract', 'Texas REALTORS® forms library', 'TX · association transaction forms', 'Texas REALTORS®', 'https://www.texasrealestate.com/realtorforms/', 'licensed_association', 'Association access and use are governed by Texas REALTORS® provider terms. NEOH opens the source; it does not copy or rehost the form text.', 'approved'),
    ('utah-realtors-forms', 'state', 'UT', 'contract', 'Utah REALTORS® forms library', 'UT · association transaction forms', 'Utah REALTORS®', 'https://utahrealtors.com/forms/', 'licensed_association', 'Association access and use are governed by Utah REALTORS® provider terms. NEOH opens the source; it does not copy or rehost the form text.', 'approved'),
    ('wyoming-realtors-forms-contracts', 'state', 'WY', 'contract', 'Wyoming REALTORS® forms and contracts', 'WY · member transaction forms', 'Wyoming REALTORS®', 'https://www.wyomingrealtors.org/benefits', 'licensed_association', 'Member forms are delivered through approved transaction platforms. NEOH opens the source; it does not copy or rehost the form text.', 'approved'),

    -- National government portals are intentionally separate from state rows.
    ('federal-hud-forms-library', 'federal', NULL, 'document', 'HUD forms library', 'US federal · HUD forms and guidance', 'U.S. Department of Housing and Urban Development', 'https://www.hud.gov/program_offices/administration/hudclips/forms', 'public_portal', 'Official federal source. Use the source’s current instructions and program requirements.', 'approved'),
    ('federal-hud-single-family-model-documents', 'federal', NULL, 'document', 'HUD single-family model documents', 'US federal · FHA/HUD model documents', 'U.S. Department of Housing and Urban Development', 'https://www.hud.gov/hud-partners/single-family-model-documents', 'public_portal', 'Official federal source. Use the source’s current instructions and program requirements.', 'approved'),
    ('federal-cfpb-trid-forms', 'federal', NULL, 'document', 'CFPB loan estimate and closing disclosure forms', 'US federal · TRID model forms and samples', 'Consumer Financial Protection Bureau', 'https://www.consumerfinance.gov/compliance/compliance-resources/mortgage-resources/tila-respa-integrated-disclosures/forms-samples/', 'public_portal', 'Official federal source. Use the source’s current instructions and program requirements.', 'approved'),
    ('federal-va-forms', 'federal', NULL, 'document', 'VA forms', 'US federal · VA home-loan related forms', 'U.S. Department of Veterans Affairs', 'https://www.va.gov/forms/', 'public_portal', 'Official federal source. Use the source’s current instructions and program requirements.', 'approved'),
    ('federal-usda-rural-development-forms', 'federal', NULL, 'document', 'USDA Rural Development forms', 'US federal · rural housing forms and publications', 'U.S. Department of Agriculture Rural Development', 'https://www.rd.usda.gov/resources/forms', 'public_portal', 'Official federal source. Use the source’s current instructions and program requirements.', 'approved'),
    ('federal-irs-real-estate-forms', 'federal', NULL, 'document', 'IRS real estate transaction forms', 'US federal · Form 1099-S and instructions', 'Internal Revenue Service', 'https://www.irs.gov/forms-instructions-and-publications?find=1099&page=1', 'public_portal', 'Official federal source. Use the source’s current instructions and filing requirements.', 'approved')
ON CONFLICT (source_key) DO UPDATE
SET authority_scope = EXCLUDED.authority_scope,
    state_code = EXCLUDED.state_code,
    document_kind = EXCLUDED.document_kind,
    title = EXCLUDED.title,
    subtitle = EXCLUDED.subtitle,
    source_name = EXCLUDED.source_name,
    source_url = EXCLUDED.source_url,
    access_mode = EXCLUDED.access_mode,
    access_note = EXCLUDED.access_note,
    approval_status = EXCLUDED.approval_status,
    verified_at = now(),
    updated_at = now();

-- Direct public federal PDFs are registered separately.  The API permits a
-- device download only when a reviewed record points to an HTTPS .gov source.
INSERT INTO authorized_document_sources (
    source_key, authority_scope, state_code, document_kind, title, subtitle,
    source_name, source_url, pdf_url, version, effective_date, approval_status
) VALUES
    ('cfpb-loan-estimate-model-form', 'federal', NULL, 'document', 'Loan Estimate model form', 'US federal · CFPB TRID · H-24(A)', 'Consumer Financial Protection Bureau', 'https://www.consumerfinance.gov/compliance/compliance-resources/mortgage-resources/tila-respa-integrated-disclosures/forms-samples/', 'https://files.consumerfinance.gov/f/201403_cfpb_loan-estimate_model-form-H24.pdf', 'H-24(A)', NULL, 'approved'),
    ('cfpb-closing-disclosure-model-form', 'federal', NULL, 'document', 'Closing Disclosure model form', 'US federal · CFPB TRID · H-25(A)', 'Consumer Financial Protection Bureau', 'https://www.consumerfinance.gov/compliance/compliance-resources/mortgage-resources/tila-respa-integrated-disclosures/forms-samples/', 'https://files.consumerfinance.gov/f/201403_cfpb_closing-disclosure_cover-H25A.pdf', 'H-25(A)', NULL, 'approved'),
    ('cfpb-escrow-cancellation-notice', 'federal', NULL, 'document', 'Escrow cancellation notice model form', 'US federal · CFPB TRID · H-29', 'Consumer Financial Protection Bureau', 'https://www.consumerfinance.gov/compliance/compliance-resources/mortgage-resources/tila-respa-integrated-disclosures/forms-samples/', 'https://files.consumerfinance.gov/f/201403_cfpb_escrow-cancellation_H29.pdf', 'H-29', NULL, 'approved'),
    ('irs-form-1099-s-2026', 'federal', NULL, 'document', 'Form 1099-S: Proceeds From Real Estate Transactions', 'US federal · IRS · 2026', 'Internal Revenue Service', 'https://www.irs.gov/forms-instructions-and-publications?find=1099&page=1', 'https://www.irs.gov/pub/irs-pdf/f1099s.pdf', '2026', NULL, 'approved'),
    ('hud-51971-offer-purchase-agreement', 'federal', NULL, 'contract', 'HUD-51971 Offer of Sale and Purchase Agreement', 'US federal · public-housing real property acquisition', 'U.S. Department of Housing and Urban Development', 'https://www.hud.gov/program_offices/administration/hudclips/forms/hud4', 'https://www.hud.gov/sites/dfiles/OCHCO/documents/51971-1.51971-2.pdf', 'HUD-51971', NULL, 'approved')
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
