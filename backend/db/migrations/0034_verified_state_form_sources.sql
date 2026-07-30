-- 0034_verified_state_form_sources.sql
--
-- Approved source catalog for the state picker.  This catalog intentionally
-- stores links and access facts, never association-owned form bodies.  Public
-- government PDFs are registered separately below so they can be previewed
-- and saved without giving the server arbitrary outbound-fetch capability.

CREATE TABLE IF NOT EXISTS authorized_form_source_links (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_key          text NOT NULL UNIQUE,
    state_code          char(2) NOT NULL REFERENCES state_regulatory_profiles(state_code),
    document_kind       text NOT NULL CHECK (document_kind IN ('contract', 'document')),
    title               text NOT NULL,
    subtitle            text NOT NULL DEFAULT '',
    source_name         text NOT NULL,
    source_url          text NOT NULL CHECK (source_url LIKE 'https://%'),
    access_mode         text NOT NULL CHECK (access_mode IN ('public_portal', 'licensed_association')),
    access_note         text NOT NULL DEFAULT '',
    approval_status     text NOT NULL DEFAULT 'approved'
                            CHECK (approval_status IN ('approved', 'retired')),
    verified_at         timestamptz NOT NULL DEFAULT now(),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_authorized_form_source_links_active
    ON authorized_form_source_links (state_code, document_kind)
    WHERE approval_status = 'approved';

-- Licensed association sources open at the provider.  The platform does not
-- copy, scrape, cache, proxy, or rehost their form text.  A tenant's provider
-- membership or separate commercial license remains required.
INSERT INTO authorized_form_source_links (
    source_key, state_code, document_kind, title, subtitle, source_name,
    source_url, access_mode, access_note, approval_status
) VALUES
    ('al-alabama-realtors-form-library', 'AL', 'contract', 'Alabama REALTORS® form library', 'AL · statewide transaction forms', 'Alabama REALTORS®', 'https://www.alabamarealtors.com/statewide-legal-forms', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('ak-real-estate-commission-forms', 'AK', 'document', 'Alaska Real Estate Commission forms', 'AK · consumer and transaction forms', 'Alaska Department of Commerce, Community, and Economic Development', 'https://www.commerce.alaska.gov/web/cbpl/ProfessionalLicensing/RealEstateCommission/ConsumerInformation/Forms', 'public_portal', 'Official public source. Verified direct PDFs are listed separately when available.', 'approved'),
    ('az-realtors-form-library', 'AZ', 'contract', 'Arizona REALTORS® form library', 'AZ · agency and employment forms', 'Arizona REALTORS®', 'https://www.aaronline.com/manage-risk/sample-forms/agency-and-employment-forms/', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('ar-realtors-form-ordering', 'AR', 'contract', 'Arkansas REALTORS® form library', 'AR · transaction forms', 'Arkansas REALTORS®', 'https://orderforms.arkansasrealtors.com/order-paper-forms', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('ca-car-standard-forms', 'CA', 'contract', 'C.A.R. standard forms library', 'CA · standard transaction forms', 'California Association of REALTORS®', 'https://app2.car.org/en/transactions/standard-forms/list-of-standard-forms', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('co-dre-contracts-and-forms', 'CO', 'contract', 'Colorado real estate contracts and forms', 'CO · commission-approved forms', 'Colorado Division of Real Estate', 'https://dre.colorado.gov/real-estate-broker-contracts-and-forms', 'public_portal', 'Official public source. Verified direct PDFs are listed separately when available.', 'approved'),
    ('ct-realtors-form-library', 'CT', 'contract', 'Connecticut REALTORS® form library', 'CT · member and vendor forms', 'Connecticut REALTORS®', 'https://www.ctrealtors.com/members/forms/', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('de-realtor-forms-library', 'DE', 'contract', 'Delaware REALTORS® forms library', 'DE · member transaction forms', 'Delaware Association of REALTORS®', 'https://delawarerealtor.com/login/', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('fl-realtors-form-simplicity', 'FL', 'contract', 'Florida REALTORS® Form Simplicity', 'FL · member form library', 'Florida REALTORS®', 'https://www.floridarealtors.org/tools-research/form-simplicity', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('ga-realtors-contract-forms', 'GA', 'contract', 'Georgia REALTORS® contract forms', 'GA · statewide forms', 'Georgia REALTORS®', 'https://garealtor.com/law-ethics/contract-forms/', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('hi-realtors-member-forms', 'HI', 'contract', 'Hawaiʻi REALTORS® form library', 'HI · member transaction forms', 'Hawaiʻi REALTORS®', 'https://www.hawaiirealtors.com/forms-for-members', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('idaho-realtors-forms', 'ID', 'contract', 'Idaho REALTORS® form library', 'ID · member forms', 'Idaho REALTORS®', 'https://idahorealtors.com/forms/', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('il-realtors-legal-forms', 'IL', 'contract', 'Illinois REALTORS® legal forms', 'IL · member forms and legal resources', 'Illinois REALTORS®', 'https://www.illinoisrealtors.org/legal/', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('in-realtors-member-forms', 'IN', 'contract', 'Indiana REALTORS® member forms', 'IN · member transaction forms', 'Indiana Association of REALTORS®', 'https://www.indianarealtors.com/', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('ia-iowadocs-form-library', 'IA', 'contract', 'IowaDocs® form library', 'IA · real estate transaction forms', 'Iowa Association of REALTORS®', 'https://www.iowadocs.net/', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('ks-realtors-contract-forms', 'KS', 'contract', 'Kansas Association of REALTORS® contract forms', 'KS · statewide forms', 'Kansas Association of REALTORS®', 'https://kansasrealtor.com/contracts-and-forms/', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('ky-real-estate-commission-forms', 'KY', 'document', 'Kentucky Real Estate Commission forms', 'KY · commission forms', 'Kentucky Real Estate Commission', 'https://www.krec.ky.gov/new_docs.aspx?cat=47&menuid=58', 'public_portal', 'Official public source. Verified direct PDFs are listed separately when available.', 'approved'),
    ('la-real-estate-commission-mandatory-forms', 'LA', 'contract', 'Louisiana mandatory real estate forms', 'LA · commission mandatory forms', 'Louisiana Real Estate Commission', 'https://lrec.gov/form-categories/mandatory', 'public_portal', 'Official public source. Verified direct PDFs are listed separately when available.', 'approved'),
    ('me-realtors-member-resources', 'ME', 'contract', 'Maine REALTORS® form resources', 'ME · member transaction forms', 'Maine Association of REALTORS®', 'https://www.mainerealtors.com/member-resources/', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('md-realtors-forms', 'MD', 'contract', 'Maryland REALTORS® forms', 'MD · member legal forms', 'Maryland REALTORS®', 'https://www.mdrealtor.org/Legal-Resources/Forms/Emergency-Forms', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('ma-realtors-member-forms', 'MA', 'contract', 'Massachusetts REALTORS® member forms', 'MA · member transaction forms', 'Massachusetts Association of REALTORS®', 'https://www.marealtor.com/', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('mi-realtors-member-forms', 'MI', 'contract', 'Michigan REALTORS® form library', 'MI · purchase agreements and transaction forms', 'Michigan REALTORS®', 'https://www.mirealtors.com/Legal-Resources/Forms-2024', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('mn-realtors-legal-forms', 'MN', 'contract', 'Minnesota REALTORS® legal forms', 'MN · member transaction forms', 'Minnesota REALTORS®', 'https://www.mnrealtor.com/member-services/legal-affairs/forms', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('ms-real-estate-commission-forms', 'MS', 'document', 'Mississippi Real Estate Commission forms', 'MS · commission forms', 'Mississippi Real Estate Commission', 'https://www.mrec.ms.gov/forms/', 'public_portal', 'Official public source. Verified direct PDFs are listed separately when available.', 'approved'),
    ('mo-realtors-standard-forms', 'MO', 'contract', 'Missouri REALTORS® standard forms', 'MO · standard transaction forms', 'Missouri REALTORS®', 'https://www.missourirealtor.org/missourirealtors/risk-management/standard-forms', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('mt-realtors-forms-and-legal', 'MT', 'contract', 'Montana REALTORS® forms and legal resources', 'MT · transaction forms', 'Montana Association of REALTORS®', 'https://www.montanarealtors.org/formsandlegalinfo/', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('ne-real-estate-commission-forms', 'NE', 'contract', 'Nebraska Real Estate Commission forms', 'NE · commission transaction forms', 'Nebraska Real Estate Commission', 'https://nrec.nebraska.gov/licensing-forms/formlist.html', 'public_portal', 'Official public source. Verified direct PDFs are listed separately when available.', 'approved'),
    ('nv-real-estate-division-disclosures', 'NV', 'document', 'Nevada Real Estate Division disclosures', 'NV · consumer and property disclosures', 'Nevada Real Estate Division', 'https://red.nv.gov/Content/Forms/Disclosures/', 'public_portal', 'Official public source. Verified direct PDFs are listed separately when available.', 'approved'),
    ('nh-realtors-form-library', 'NH', 'contract', 'New Hampshire REALTORS® form library', 'NH · transaction forms', 'New Hampshire REALTORS®', 'https://nhar.org/forms', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('nj-realtor-zipform', 'NJ', 'contract', 'New Jersey REALTORS® ZipForm library', 'NJ · member transaction forms', 'New Jersey REALTORS®', 'https://www.njrealtor.com/zipform/', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('nm-realtors-member-forms', 'NM', 'contract', 'New Mexico REALTORS® purchase agreement', 'NM · residential resale purchase agreement', 'New Mexico Association of REALTORS®', 'https://www.nmrealtor.com/wp-content/uploads/2025/01/NMAR-2104-Purchase-Agreement-Residential-2024-DEC.pdf', 'licensed_association', 'Licensed access required. Open the provider source; NEOH does not copy or rehost association form text.', 'approved'),
    ('ny-department-of-state-forms', 'NY', 'document', 'New York Department of State real estate forms', 'NY · licensing and disclosure forms', 'New York Department of State', 'https://dos.ny.gov/additional-forms-real-estate-salesperson', 'public_portal', 'Official public source. Verified direct PDFs are listed separately when available.', 'approved'),
    ('nc-realtors-member-forms', 'NC', 'contract', 'North Carolina REALTORS® forms index', 'NC · jointly approved transaction forms', 'North Carolina REALTORS®', 'https://www.ncrealtors.org/wp-content/uploads/formslist.pdf', 'licensed_association', 'Licensed access required. Open the provider source; NEOH does not copy or rehost association form text.', 'approved'),
    ('nd-realtors-member-forms', 'ND', 'contract', 'North Dakota REALTORS® purchase agreement', 'ND · statewide purchase agreement', 'North Dakota Association of REALTORS®', 'https://www.ndrealtors.com/wp-content/uploads/2025/01/Purchase-Agreement.pdf', 'licensed_association', 'Licensed access required. Open the provider source; NEOH does not copy or rehost association form text.', 'approved'),
    ('oh-realtors-member-portal', 'OH', 'contract', 'Ohio REALTORS® form portal', 'OH · member transaction forms', 'Ohio REALTORS®', 'https://portal.ohiorealtors.org/', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('ok-real-estate-commission-contract-forms', 'OK', 'contract', 'Oklahoma Real Estate Commission contract forms', 'OK · commission contract forms', 'Oklahoma Real Estate Commission', 'https://www.oklahoma.gov/orec/contract-forms-and-related-addenda.html', 'public_portal', 'Official public source. Verified direct PDFs are listed separately when available.', 'approved'),
    ('or-oref-library', 'OR', 'contract', 'OREF form library', 'OR · Oregon real estate forms', 'Oregon Real Estate Forms', 'https://orefonline.com/oref-library/', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('pa-realtors-standard-forms', 'PA', 'contract', 'Pennsylvania REALTORS® standard forms', 'PA · standard transaction forms', 'Pennsylvania Association of REALTORS®', 'https://www.parealtors.org/standard-forms/', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('ri-realtors-forms', 'RI', 'contract', 'Rhode Island REALTORS® forms', 'RI · member transaction forms', 'Rhode Island Association of REALTORS®', 'https://www.rirealtors.org/riar-forms', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('sc-real-estate-commission-resources', 'SC', 'document', 'South Carolina Real Estate Commission resources', 'SC · commission forms and disclosures', 'South Carolina Real Estate Commission', 'https://www.llr.sc.gov/re/resources.aspx', 'public_portal', 'Official public source. Verified direct PDFs are listed separately when available.', 'approved'),
    ('sd-real-estate-transaction-forms', 'SD', 'contract', 'South Dakota real estate transaction forms', 'SD · commission forms', 'South Dakota Department of Labor and Regulation', 'https://dlr.sd.gov/realestate/real_estate_transaction_forms.aspx', 'public_portal', 'Official public source. Verified direct PDFs are listed separately when available.', 'approved'),
    ('tn-realtors-member-services', 'TN', 'contract', 'Tennessee REALTORS® member forms', 'TN · member transaction forms', 'Tennessee REALTORS®', 'https://tnrealtors.com/members/member-services/', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('tx-real-estate-commission-forms', 'TX', 'contract', 'Texas Real Estate Commission forms', 'TX · state-approved contracts and forms', 'Texas Real Estate Commission', 'https://www.trec.texas.gov/forms', 'public_portal', 'Official public source. Verified direct PDFs are listed separately when available.', 'approved'),
    ('ut-state-approved-real-estate-forms', 'UT', 'contract', 'Utah state-approved real estate forms', 'UT · state-approved contract forms', 'Utah Division of Real Estate', 'https://commerce.utah.gov/realestate/real-estate/forms/state-approved/', 'public_portal', 'Official public source. Verified direct PDFs are listed separately when available.', 'approved'),
    ('vt-realtors-transaction-tools', 'VT', 'contract', 'Vermont REALTORS® transaction tools', 'VT · member transaction forms', 'Vermont Association of REALTORS®', 'https://www.vermontrealtors.com/transaction-tools/', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('va-realtors-residential-contract', 'VA', 'contract', 'Virginia REALTORS® residential contract forms', 'VA · member transaction forms', 'Virginia REALTORS®', 'https://virginiarealtors.org/law-ethics/new-residential-purchase-contract/', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('wa-nwmls-forms', 'WA', 'contract', 'Northwest Multiple Listing Service forms', 'WA · subscription transaction forms', 'Northwest Multiple Listing Service', 'https://www.nwmls.com/', 'licensed_association', 'Licensed access required. Open the provider portal; NEOH does not copy or rehost association form text.', 'approved'),
    ('wv-real-estate-division-forms', 'WV', 'document', 'West Virginia Real Estate Division forms', 'WV · commission forms', 'West Virginia Real Estate Division', 'https://realestatedivision.wv.gov/forms', 'public_portal', 'Official public source. Verified direct PDFs are listed separately when available.', 'approved'),
    ('wi-real-estate-contractual-forms', 'WI', 'contract', 'Wisconsin real estate contractual forms', 'WI · board-approved forms', 'Wisconsin Department of Safety and Professional Services', 'https://dsps.wi.gov/Pages/BoardsCouncils/RealEstate/ContractualForms/Forms.aspx', 'public_portal', 'Official public source. Verified direct PDFs are listed separately when available.', 'approved'),
    ('wy-real-estate-professional-forms', 'WY', 'document', 'Wyoming real estate professional forms', 'WY · commission forms', 'Wyoming Real Estate Commission', 'https://realestate.wyo.gov/real-estate-professionals/applications-and-forms', 'public_portal', 'Official public source. Verified direct PDFs are listed separately when available.', 'approved')
ON CONFLICT (source_key) DO UPDATE
SET state_code = EXCLUDED.state_code,
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

-- Direct public PDFs are separate records because only government-hosted PDFs
-- may be downloaded through the backend.  These sources remain source links,
-- not imported form content.
INSERT INTO authorized_document_sources (
    source_key, authority_scope, state_code, document_kind, title, subtitle,
    source_name, source_url, pdf_url, version, effective_date, approval_status
) VALUES
    ('al-rec-brokerage-services-disclosure-2025', 'state', 'AL', 'document', 'Real Estate Brokerage Services Disclosure', 'AL · consumer disclosure · 2025', 'Alabama Real Estate Commission', 'https://arec.alabama.gov/pages/faq.aspx?AspxAutoDetectCookieSupport=1', 'https://arec.alabama.gov/docs/RECADForm09192025.pdf', '09-19-2025', NULL, 'approved'),
    ('ak-rec-property-transfer-disclosure-2024', 'state', 'AK', 'document', 'Residential Real Property Transfer Disclosure Statement', 'AK · Form 08-4229 · 2024', 'Alaska Real Estate Commission', 'https://www.commerce.alaska.gov/web/cbpl/ProfessionalLicensing/RealEstateCommission/ConsumerInformation/Forms', 'https://www.commerce.alaska.gov/web/Portals/5/pub/rec4229.pdf', '08-4229', NULL, 'approved'),
    ('azre-buyer-advisory-2025', 'state', 'AZ', 'document', 'Buyer Advisory', 'AZ · January 2025', 'Arizona Department of Real Estate', 'https://azre.gov/resources/buyer-advisory', 'https://azre.gov/sites/default/files/2025-08/Buyer%20Advisory%20January%202025.pdf', '2025-01', NULL, 'approved'),
    ('ar-rec-agency-representation-2024', 'state', 'AR', 'document', 'Agency Representation Pamphlet', 'AR · December 2024', 'Arkansas Real Estate Commission', 'https://arec.arkansas.gov/forms-publications/forms/', 'https://arec.arkansas.gov/wp-content/uploads/AgencyRepBrochure122024.pdf', '12-2024', NULL, 'approved'),
    ('ca-dre-subdivision-interest-disclosure-2023', 'state', 'CA', 'document', 'Existing Subdivision Interest Disclosure Form', 'CA · RE 640 · revised 2023', 'California Department of Real Estate', 'https://www.dre.ca.gov/Publications/CompleteListPublications.html', 'https://dre.ca.gov/files/pdf/forms/re640.pdf', 'RE-640', NULL, 'approved'),
    ('ct-dcp-property-condition-report-2025', 'state', 'CT', 'document', 'Residential Property Condition Report', 'CT · revised July 2025', 'Connecticut Department of Consumer Protection', 'https://portal.ct.gov/DCP/Consumer/Consumers---Real-Estate', 'https://portal.ct.gov/dcp/-/media/dcp/forms/residential-property-condition-report.pdf?hash=E1583B20230EDB8AC220C0B6C3876BB1&rev=3c1f0f26d80f4e7c83eb63d887510e15', '07-2025', NULL, 'approved'),
    ('de-drec-seller-property-condition-2025', 'state', 'DE', 'document', 'Seller’s Disclosure of Real Property Condition Report', 'DE · effective August 2025', 'Delaware Real Estate Commission', 'https://dpr.delaware.gov/boards/realestate/forms/', 'https://dprfiles.delaware.gov/realestate/FINAL_DREC%20Sellers%20Disclosure%20for%20Real%20Property%20Condition%20Report%209_17_2024.pdf', '09-17-2024', DATE '2025-08-01', 'approved'),
    ('hi-reb-working-with-a-broker', 'state', 'HI', 'document', 'Working with a Real Estate Broker', 'HI · consumer brochure', 'Hawaii Real Estate Branch', 'https://cca.hawaii.gov/reb/resources-for-consumers/', 'https://files.hawaii.gov/dcca/reb/real_ed/re_other/workingwitharebroker.pdf', NULL, NULL, 'approved'),
    ('id-dopl-agency-disclosure-2025', 'state', 'ID', 'document', 'Agency Disclosure Brochure', 'ID · effective July 2025', 'Idaho Division of Occupational and Professional Licenses', 'https://dopl.idaho.gov/real-estate/', 'https://dopl.idaho.gov/wp-content/uploads/2025/06/2025-Agency-Disclosure-Brochure-FINAL.pdf', '2025-07', DATE '2025-07-01', 'approved'),
    ('ky-krec-seller-property-condition', 'state', 'KY', 'document', 'Seller’s Disclosure of Property Condition', 'KY · KREC Form 402', 'Kentucky Real Estate Commission', 'https://www.krec.ky.gov/new_docs.aspx?cat=47&menuid=58', 'https://www.krec.ky.gov/Documents/KREC%20Form%20402%20-%20Sellers%20Disclosure%20of%20Property%20Condition.pdf', '402', NULL, 'approved'),
    ('me-property-disclosure-information', 'state', 'ME', 'document', 'Property Disclosure Information', 'ME · commission publication', 'Maine Real Estate Commission', 'https://www1.maine.gov/pfr/professionallicensing/professions/real-estate-commission/home/news-publications', 'https://www.maine.gov/pfr/professionallicensing/sites/maine.gov.pfr.professionallicensing/files/inline-files/Disclosures_0.pdf', NULL, NULL, 'approved'),
    ('md-residential-property-disclosure', 'state', 'MD', 'document', 'Maryland Residential Property Disclosure and Disclaimer Statement', 'MD · residential property disclosure', 'Maryland Department of Labor', 'https://labor.maryland.gov/license/mrec/', 'https://www.labor.maryland.gov/forms/propertydanddform.pdf', NULL, NULL, 'approved'),
    ('ma-licensee-consumer-relationship-disclosure', 'state', 'MA', 'document', 'Mandatory Licensee-Consumer Relationship Disclosure', 'MA · mandatory consumer disclosure', 'Massachusetts Board of Registration of Real Estate Brokers and Salespersons', 'https://www.mass.gov/info-details/re39r25-commonly-used-forms-residential-mandatoryoptional', 'https://www.mass.gov/doc/massachusettss-mandatory-licensee-consumer-relationship-disclosure/download', NULL, NULL, 'approved'),
    ('mn-agency-disclosure-statute', 'state', 'MN', 'document', 'Agency Disclosure Requirements', 'MN · Minn. Stat. § 82.67', 'Minnesota Revisor of Statutes', 'https://www.revisor.mn.gov/statutes/cite/82.67', 'https://www.revisor.mn.gov/statutes/cite/82.67/pdf', NULL, NULL, 'approved'),
    ('ms-mrec-working-with-a-broker-2023', 'state', 'MS', 'document', 'Working with a Real Estate Broker', 'MS · Agency Disclosure Form A', 'Mississippi Real Estate Commission', 'https://www.mrec.ms.gov/forms/', 'https://www.mrec.ms.gov/wp-content/uploads/2023/06/WWREB-fillable-Legal-size-06-05-2023.pdf', '06-05-2023', NULL, 'approved'),
    ('ne-nrec-agency-disclosure-2025', 'state', 'NE', 'document', 'Agency Disclosure Information for Buyers and Sellers', 'NE · updated October 2025', 'Nebraska Real Estate Commission', 'https://nrec.nebraska.gov/legal/brokeragerelationshipinfo.html', 'https://nrec.nebraska.gov/pdf/forms/AgencyDisclosureBuyerSeller2025.pdf', '10-2025', NULL, 'approved'),
    ('nj-dobi-consumer-information-statement-2024', 'state', 'NJ', 'document', 'Consumer Information Statement on New Jersey Real Estate Relationships', 'NJ · Bulletin 24-11', 'New Jersey Department of Banking and Insurance', 'https://www.nj.gov/dobi/division_consumers/realestate/re_menu.htm', 'https://www.nj.gov/dobi/bulletins/blt24_11Info.pdf', '24-11', NULL, 'approved'),
    ('or-rea-initial-agency-disclosure', 'state', 'OR', 'document', 'Initial Agency Disclosure Pamphlet', 'OR · real estate agency disclosure', 'Oregon Real Estate Agency', 'https://www.oregon.gov/rea/licensing/Pages/Initial-Agency-Disclosure.aspx', 'https://www.oregon.gov/rea/licensing/Documents/Initial-Agency-Disclosure-Pamphlet.pdf', NULL, NULL, 'approved'),
    ('sc-rec-brokerage-relationship-disclosure', 'state', 'SC', 'document', 'Disclosure of Real Estate Brokerage Relationships', 'SC · brokerage disclosure', 'South Carolina Real Estate Commission', 'https://www.llr.sc.gov/re/resources.aspx', 'https://www.llr.sc.gov/re/recpdf/Doc170%20SC_Disclosure_of_Real_Estate_Brokerage_Relationships.pdf', 'Doc-170', NULL, 'approved'),
    ('sd-dlr-real-estate-relationships-disclosure', 'state', 'SD', 'document', 'Real Estate Relationships Disclosure', 'SD · transaction disclosure', 'South Dakota Department of Labor and Regulation', 'https://dlr.sd.gov/realestate/real_estate_transaction_forms.aspx', 'https://dlr.sd.gov/realestate/forms/real_estate_relationships_disclosure.pdf', NULL, NULL, 'approved'),
    ('ut-dre-real-estate-purchase-contract', 'state', 'UT', 'contract', 'Real Estate Purchase Contract', 'UT · state-approved REPC', 'Utah Division of Real Estate', 'https://commerce.utah.gov/realestate/real-estate/forms/state-approved/', 'https://commerce.utah.gov/wp-content/uploads/2023/03/purchase-contract.pdf', 'REPC', NULL, 'approved'),
    ('va-dpor-property-disclosure-statement', 'state', 'VA', 'document', 'Residential Property Disclosure Statement', 'VA · residential property disclosure', 'Virginia Department of Professional and Occupational Regulation', 'https://www.dpor.virginia.gov/Consumers/Disclosure_Forms/', 'https://www.dpor.virginia.gov/sites/default/files/boards/Real_Estate/Title55.1_REB_ResidentialPropertyDisclosureStatement.pdf', NULL, NULL, 'approved'),
    ('wi-reb-wb11-residential-offer-2024', 'state', 'WI', 'contract', 'WB-11 Residential Offer to Purchase', 'WI · board-approved form · 2024', 'Wisconsin Real Estate Examining Board', 'https://dsps.wi.gov/Pages/BoardsCouncils/RealEstate/ContractualForms/Forms.aspx', 'https://dsps.wi.gov/Documents/BoardCouncils/REB/Forms/WB-11.pdf', 'WB-11', DATE '2024-08-15', 'approved')
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

ALTER TABLE authorized_form_source_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE authorized_form_source_links FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS authorized_form_source_links_authenticated_read
    ON authorized_form_source_links;
CREATE POLICY authorized_form_source_links_authenticated_read
    ON authorized_form_source_links
    FOR SELECT
    USING (app_current_tenant() IS NOT NULL OR app_is_platform_admin());

GRANT SELECT ON authorized_form_source_links TO oracle_app;
