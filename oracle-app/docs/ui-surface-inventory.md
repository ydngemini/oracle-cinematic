# UI surface inventory

Mission 3, section 27. This lists every UI component in `src/components` (including `motion/`) and `src/neoh`, says how a user reaches it, and gives it one class.

Reachability was worked out from a static and dynamic import graph rooted at `src/main.jsx` (tests excluded), then checked against the render conditions in `CrmShell.jsx`, `routes.js`, `UniversalWorkspace.jsx`, `OurAITab.jsx`, `SalesWorkspace.jsx`, `DealsTab.jsx`, `PropertiesTab.jsx`, `PeopleTab.jsx` and `MyProfileTab.jsx`. The baseline is main @ `ea22da3`. Section 9 records what changed afterwards.

| Class | Meaning |
|---|---|
| **PRIMARY** | Part of the Home / Work / Neoh spine. Every user sees it. |
| **CONTEXTUAL** | Reached from a primary surface: a Work view, a sub-tab, a sheet or a drawer. |
| **ADMIN** | Gated by role (`platform_admin`, or `broker_owner` where noted). |
| **PUBLIC** | Shown on an unauthenticated route, where the token in the URL is the whole capability. |
| **DUPLICATE** | A second home for something that already exists elsewhere. |
| **LEGACY** | Still mounted, but carries the old HUD / mission-control identity or a retired workflow. |
| **UNREACHABLE** | Nothing in the running app mounts it, or only a hand-typed URL does. |

Path notation: `Home`, `Work` and `Neoh` are the three `TabBar` destinations from `CrmShell` `TABS`. `Work · <chip>` means a kind chip in `UniversalWorkspace`; the chips are People, Properties, Deals and Conversations. `Profile` means the avatar button in the shell header, which opens the profile sheet (`CrmShell` `profileViews`). `Neoh · <tab>` means a workspace tab inside `OurAITab`, which is what the Neoh top-level tab renders today (`CrmShell.jsx:504`, `initialWorkspace="cowork"`).

---

## 1. Shell and primary spine

| Component | Class | How it is reached | Reason |
|---|---|---|---|
| `CrmShell` | PRIMARY | Mounted by `App.jsx` after sign-in and the policy gate | The authenticated shell: header, TabBar, profile sheet and the entity-sheet overlay. |
| `TabBar` | PRIMARY | `CrmShell` | Home / Work / Neoh navigation. |
| `NeohBrandMark` | PRIMARY | `CrmShell` header | The product mark. |
| `StateSelector` | PRIMARY | `CrmShell` header | Picks the jurisdiction or market for the session. |
| `ServiceStatusBanner` | PRIMARY | `CrmShell` | Customer-facing degradation banner. |
| `NeohFooter` | PRIMARY | `CrmShell` | Footer. |
| `ProductTour` (+ `useSpotlight`) | PRIMARY | `CrmShell`: first run, or replayed | Guided first-run tour. |
| `BillingOverlay` | PRIMARY | `CrmShell`, always mounted | Paywall or lapsed-plan overlay. |
| `OnboardingGate` | PRIMARY | `CrmShell`, always mounted | First-run setup gate. |
| `ErrorBoundary` | PRIMARY | `main.jsx` (whole app), plus `CrmShell`, `DealsTab`, `PropertiesTab` and `EntitySheet` with a label | Recovery boundary. |
| `PolicyAcceptanceGate` | PRIMARY | `App.jsx` `AuthedApp` | Terms and policy acceptance before the CRM mounts. |
| `LoginVault` | PRIMARY | `App.jsx` when unauthenticated | The sign-in screen. |
| `motion/AdaptiveViewTransition` | PRIMARY | `CrmShell`, `HouseSelection`, `ReelExperience`, `neoh/motion.js` | View-transition wrapper (infrastructure). |
| `motion/BorderBeam` | LEGACY | `CrmShell` profile sheet, `PersonalCommandComposer`, `AgentStatusBar` | An orbiting gold beam on panel borders. This is HUD furniture (section 45). |
| `motion/KineticText` | LEGACY | `AgentStatusBar` only | Glyph-scramble text effect, i.e. fake terminal output. |
| `neoh/NeohHome` | PRIMARY | `Home` (`/`) | "What matters right now". |
| `neoh/UniversalWorkspace` | PRIMARY | `Work` (`/work?q=&type=`) | Search across everything; the old tabs are views selected by `?type`. |
| `neoh/NeohSurface` | PRIMARY | `CrmShell`, always mounted | The Neoh conversation and composer surface (text and microphone). |
| `neoh/EntitySheet` | PRIMARY | `/p/:id`, `/property/:key`, `/deal/:id`, opened over the current view | Record sheet for a person, property or deal. |
| `neoh/EntityFrame`, `neoh/NeohRead` | PRIMARY | Inside `EntitySheet` | Sheet frame and Neoh's read of the record. |
| `neoh/LivingObject` | PRIMARY | `EntitySheet`, `EntityFrame`, `PeopleTab`, `neoh/primitives` | "Living" record card. |
| `neoh/NeohCorner` | PRIMARY | `CrmShell`: Home only, hidden while a sheet is open | Mascot corner. |
| `neoh/NeohCorner3D` | CONTEXTUAL | Lazy inside `NeohCorner` (agent 5) | Optional 3D mascot, so the WebGL cost is paid only when it shows. |
| `neoh/NeohAvatar`, `neoh/NeohCharacter` | PRIMARY | `NeohSurface`, `NeohCorner` | SVG mascot and its state model. |
| `neoh/NeohAvatarRive` | UNREACHABLE (in effect) | Lazy from `NeohAvatar` | There is no `.riv` asset and `rive` is not installed, so this path deliberately fails over to the SVG. |
| `neoh/Blocks`, `neoh/registry`, `neoh/primitives` | PRIMARY | `NeohSurface` → `Blocks` → `registry` → `primitives` | Renderers for Neoh's structured reply blocks. `primitives` embeds `IntelligenceFeed` and `LivingObject`. |
| `AssistantContext` | PRIMARY | `CrmShell` and many consumers | Provider for assistant command state. |
| `AssistantMessages` | PRIMARY | `NeohSurface` | Neoh message list (agent 3). |

`src/neoh` also contains pure model and hook modules, which have no UI of their own: `avatarModel`, `callPresence`, `characterGeometry`, `entityModel`, `eyeSystem`, `idleGaze`, `livingModel`, `missionModel`, `motion`, `neohModel3d`, `searchModel`, `surfaceModel`, `timeOfDay`, `useGlobalShortcuts`, `useNeohAvatarState`, `useNeohChannel` and `useSpeechInput`. All of them are reached through the components above, except `riveInputs.js`, which only a test imports (see section 10).

## 2. Work views and their sub-tabs

| Component | Class | How it is reached | Reason |
|---|---|---|---|
| `PeopleTab` | CONTEXTUAL | `Work · People` | Contacts and opportunities, with Contacts / Opportunities toggles. |
| `ClientCrmTab` | CONTEXTUAL | `Work · People` → Opportunities | Client pipeline list, embedded. |
| `ContactDetailPanel`, `ContactIntakeForm`, `ContactIntakePanel` | CONTEXTUAL | `Work · People` → open a contact, or Add | Contact detail and intake. |
| `PanelDataStatus` | CONTEXTUAL | `PeopleTab`, `StudioTab` | Shared freshness and status row. |
| `PropertiesTab` | CONTEXTUAL | `Work · Properties` | Sub-tabs: Address, Houses, Listings, MLS, Market, Forms. |
| `PropertyViewTab` | CONTEXTUAL | `Work · Properties` → Address | Address lookup, capture, tour and rehab (agent 5). |
| `HouseSelection` → `HouseWorkspace` → `HouseLinkDialog` | CONTEXTUAL | `Work · Properties` → Houses → select a house | House workspace. |
| `ListingsInventory` | CONTEXTUAL | `Work · Properties` → Listings | The agent's own listings. |
| `MlsSearch` | CONTEXTUAL | `Work · Properties` → MLS | MLS search. |
| `MarketDataPanels` | CONTEXTUAL | `Work · Properties` → Market | Market statistics. |
| `StateDocumentLibrary` | CONTEXTUAL | `Work · Properties` → Forms | State forms library. |
| `DealsTab` | CONTEXTUAL | `Work · Deals` | Sub-tabs: Pipeline, Transactions, Contracts, Portfolio, Marketplace. |
| `DealPipeline` (+ `pipelineUtils`) | CONTEXTUAL | `Work · Deals` → Pipeline | Lead pipeline: map, board and dossier. |
| `PipelineBoard` | CONTEXTUAL | `Work · Deals` → Pipeline → Board | Kanban board. |
| `LeadMap` | CONTEXTUAL | `Work · Deals` → Pipeline (map pane) | Lead map. |
| `LegalMatrix` | LEGACY | `Work · Deals` → Pipeline, shown when a lead has a `legalPackage` | Captioned "LEGAL MATRIX" in mission-control style (section 45 copy). |
| `DealBook` | CONTEXTUAL | `Work · Deals` → Transactions | Transactions, offers and milestones. |
| `DealRoomPanel` | CONTEXTUAL | `DealBook` → a transaction, or `EntitySheet` for `/deal/:id` | Deal room. |
| `ComplianceChecklistPanel` | CONTEXTUAL | `DealBook` → a transaction | Compliance checklist. |
| `ContractVaultTab` | CONTEXTUAL | `Work · Deals` → Contracts | Contract vault. |
| `ContractTemplateRegistry`, `ContractDraftWorkspace` → `ContractDraftWorkspaceView`, `ContractDocumentPanel`, `PdfDocumentPicker`, `GovInfoSearch` | CONTEXTUAL | `Work · Deals` → Contracts | Contract tooling. |
| `StateDocumentChecklist` | CONTEXTUAL | Contracts, or Client drawer → Documents | Per-client state document checklist. |
| `PortfolioTab` | CONTEXTUAL | `Work · Deals` → Portfolio | Portfolio. |
| `MarketplaceBrowse` | CONTEXTUAL | `Work · Deals` → Marketplace | Marketplace, which is empty by design without a feed. |
| `CommsTab` | CONTEXTUAL | `Work · Conversations` | Conversations inbox. |
| `CommsComposer`, `CommsShared` | CONTEXTUAL | `Work · Conversations` | Composer and shared helpers. |
| `CommandApprovalPanel` | CONTEXTUAL | `Work · Conversations` (top), and `Profile → AI controls` | Approval queue for staged sends. It has two homes; see `PersonalAITab`. |
| `IntelligenceFeed` | CONTEXTUAL / DUPLICATE | `Home` → "N more" → `/work?type=opportunities`. Also `Neoh · Intelligence` (`OurAITab`), `CommandCenter`, and inline in Neoh blocks (`primitives`) | The same ranked feed has four mount points. The canonical one is `Work?type=opportunities`. |
| `neoh/MissionBuilder` | UNREACHABLE (via UI) | Only a hand-typed `/work?type=missions`. No chip, Home link or tour step points to it | Automation builder with no navigation entry. |

## 3. Contextual sheets and drawers

| Component | Class | How it is reached | Reason |
|---|---|---|---|
| `ClientDetailDrawer` | CONTEXTUAL | `Work · People` → Opportunities → a client, or `EntitySheet` for `/p/:id` | Client drawer with Overview, Intelligence, Timeline, Tasks, Notes, Documents and Dossier tabs. |
| `ClientShared`, `ClientTimeline`, `ClientTaskList`, `ClientNotes`, `ShowingLogger` | CONTEXTUAL | Client drawer tabs | Drawer sub-panes. `ClientTaskList` embeds `PersonalCommandComposer` in compact form. |
| `RelationshipIntelligence` | CONTEXTUAL | Client drawer → Intelligence | Relationship read. |
| `DossierPanel` | CONTEXTUAL | `Work · Deals` → Pipeline → a lead, `PipelineBoard` → card, or `EntitySheet` for `/property/:key` | Property and lead dossier. Before this mission it was also pulled statically into the entry chunk through the barrel. |
| `DealIntakePanel`, `PropertyIntelligencePanel`, `IntelligenceAuthoring`, `PublicRecordsDiligence`, `PropertyMediaUploader` | CONTEXTUAL | `DossierPanel` sections | Dossier sub-panels. |
| `RehabEditorDrawer` → `FloorplanCanvas` | CONTEXTUAL | `DossierPanel` → Rehab | Rehab estimate and floor plan (agent 5). |
| `TourViewer` → `PanoViewer` / `PropertyTourViewer` / `WalkableSplatViewer` | CONTEXTUAL | `DossierPanel`, `HouseWorkspace`, `PropertyViewTab`, `EntitySheet`, `SecureDossierPage` | Tiered tour viewer that loads its engines lazily (agent 5). |
| `PropertyTour` | CONTEXTUAL | `HouseWorkspace` → Tour | 3D Tiles exterior tour (agent 5). |
| `CaptureSessionPanel` | CONTEXTUAL | `Work · Properties` → Address → capture | Capture session (agent 5). |

## 4. Neoh tab (`OurAITab`), Sales and Comms

| Component | Class | How it is reached | Reason |
|---|---|---|---|
| `OurAITab` | DUPLICATE / LEGACY | `Neoh` top-level tab (`initialWorkspace="cowork"`), or `/work?type=ai\|sales\|social\|homeowners\|automations\|sites` | A nine-tab "Our AI" console (Command, Intelligence, Cowork, Sales, Social, Homeowners, Automations, Sites, Autonomy) that stands in where the full-screen Neoh conversation should be (`CrmShell.jsx:502` comment: "lands in U7"). It duplicates `NeohSurface` as the AI home. Agent 3 is moving it. |
| `CommandCenter` | DUPLICATE | `Neoh · Command` | A second "home" dashboard. It also embeds `IntelligenceFeed`, so it overlaps both `NeohHome` and `Work?type=opportunities`. |
| `AutonomyControls` | CONTEXTUAL | `Neoh · Autonomy` | What the AI may do unattended. Keep it, but it needs a non-duplicate home. |
| `PersonalAITab` | DUPLICATE | Two entry points: `Profile → AI controls`, and `Neoh · Automations` | One screen with two homes. It embeds `PersonalCommandComposer`, `CommandApprovalPanel` and `BrokerageOnboardingPanel`, and the last two each have another home too. |
| `PersonalCommandComposer` | LEGACY | `PersonalAITab`, and `ClientTaskList` (compact) | A third composer next to `NeohSurface`. It renders `AgentStatusBar` with HUD glass and reticle classes (agent 3). |
| `AgentStatusBar` | LEGACY | Only via `PersonalCommandComposer`, plus the barrel re-export | Fake terminal phase labels (`SCOUTING_MATRIX`, `SPATIAL_STAGING`, `VOICE_NEGOTIATION`, `MEMORY SYNC: ACTIVE`) and a `KineticText` scramble. |
| `StudioTab` | CONTEXTUAL | `Neoh · Sites` (`StudioTab embedded`) | Sites and video studio. |
| `VideoStudioPanel` | CONTEXTUAL | `Neoh · Sites` → Video | Gated by the backend video-studio feature. |
| `SitePublishPanel` | CONTEXTUAL | `Neoh · Sites` → a site → Manage | Site publishing. |
| `SalesWorkspace` | CONTEXTUAL | `Neoh · Sales`, or `/work?type=sales` | Sales hub. Its sub-pages sit four levels deep: Neoh tab → Sales workspace → hub card → page. |
| `SalesAgentPage` | CONTEXTUAL (buried) | `Neoh · Sales` → Sales Agent (`?sales=/our-ai/sales/agent`) | AI sales agent configuration. |
| `PowerDialerPage` | CONTEXTUAL (buried) | `Neoh · Sales` → Power Dialer | Dialer. It also mounts `OutreachConsentPanel` and `LiveTranscript`. |
| `OutreachConsentPanel` | CONTEXTUAL | Inside `PowerDialerPage` | TCPA consent capture. |
| `LiveTranscript` | LEGACY | Only inside `PowerDialerPage` (plus the barrel) | "Cinematic mission log": `ORCL_VOICE_LINK_ACTIVE`, "Whisper instruction to AI Closer…", a `JARVIS` speaker label, scan-track telemetry, GREEN/AMBER/RED margin tags. |
| `SmartPlansPage` | CONTEXTUAL (buried) | `Neoh · Sales` → Smart Plans | Drip and automation plans. |
| `ProviderDeliveryPage` | CONTEXTUAL (buried) | `Neoh · Sales` → Providers | Bring-your-own Twilio, SMTP and other credentials. |
| `LeadRoutingPage` | CONTEXTUAL (buried) | `Neoh · Sales` → Lead Routing | Lead-routing rules and webhook docs. |

## 5. Profile sheet (settings)

| Component | Class | How it is reached | Reason |
|---|---|---|---|
| `MyProfileTab` | CONTEXTUAL | `Profile → Settings` | Agent and brokerage settings. |
| `LicenseStatusWidget` | CONTEXTUAL | `Profile → Settings` | State licences. |
| `BrokerageSetupPanel` (+ `OffboardMemberForm`) | CONTEXTUAL | `Profile → Settings` | Brokerage profile, team and integration readiness (section 69 surface). |
| `BrokerageOnboardingPanel` | CONTEXTUAL / DUPLICATE | `Profile → Settings`, and again inside `PersonalAITab` (`Profile → AI controls` and `Neoh · Automations`) | One panel with up to three mount points. |
| `AccountDataPanel` | CONTEXTUAL | `Profile → Settings` → Your data | Privacy export and erasure. |
| `HarvestControl` | ADMIN | `Profile → Settings`, only when `oracle_role === 'broker_owner'`; also `Profile → Admin` | Public-records harvest controls. |

## 6. Admin

| Component | Class | How it is reached | Reason |
|---|---|---|---|
| `AdminOpsTab` | ADMIN | `Profile → Admin`, only when `oracle_role === 'platform_admin'` (`CrmShell.jsx:392`) | Operator console. |
| `BillingUsagePanel`, `DataSourceHealthPanel`, `RoleChangePanel` | ADMIN | Inside `AdminOpsTab` | Operator panels. `src/components/admin/*` belongs to agent 1. |

## 7. Public (unauthenticated) routes

All of these are branched in `App.jsx` before the auth shell.

| Component | Class | How it is reached | Reason |
|---|---|---|---|
| `PropertyUploadPage` | PUBLIC | URL `/property-upload/<token>` | A client uploads photos for a property. |
| `SecureDossierPage` | PUBLIC | URL `/vault/secure-access/<token>` | The homeowner's dossier (embeds `TourViewer`). |
| `AcceptInvitePage` | PUBLIC | URL `/accept-invite?token=` | An invited agent sets up an account. |
| `ReelExperience` | PUBLIC | URL `/reel` or `/reel/*` | Marketing reel. Statically imported by `App.jsx`. |
| `SitePreview` | PUBLIC | URL `/site-preview/*` | Preview of an agent website. Statically imported by `App.jsx`. |

## 8. Legacy and unreachable (baseline)

| Component | Class | Proof | Reason |
|---|---|---|---|
| `AssistantRecordPicker` | UNREACHABLE | No importer anywhere in `src`, tests included. The only mention is a comment in `ListingsInventory.jsx:11` | Superseded record picker for the old assistant shell. It used `AssistantShell.module.css`. |
| `LivePulse` | UNREACHABLE | Only `components/index.js:8` re-exports it, and no consumer imports it. The only other mention is a comment in `state/useOracleWebSocket.js:210` | Old activity "pulse" HUD widget. |
| `PropertySpecs` | UNREACHABLE | Only `components/index.js:6` re-exports it, and no consumer imports it | Old spec card. |
| `SubscriptionBadge` | UNREACHABLE | Only `components/index.js:14` re-exports it, and no consumer imports it | Old plan badge. Billing is a single plan, and `BillingOverlay` covers state. |
| `components/index.js` (barrel, now deleted) | LEGACY | Only `App.jsx:3` (`CrmShell`, `LoginVault`) and `main.jsx:4` (`ErrorBoundary`) consume it | Its static re-exports of `AgentStatusBar`, `LiveTranscript` and `DossierPanel` pulled their code and stale strings ("ORCL", "AI Closer", "SCOUTING_MATRIX", "JARVIS") into the entry chunk. |
| `useJarvisVoice` (in `App.jsx`) | LEGACY / dead | It builds a `SpeechRecognition` but never calls `start()`, so the `JARVIS` transcript dispatch can never fire | A dead voice path that predates `neoh/useSpeechInput`. |

---

## 9. Post-change state (agent 2, Mission 3) — verified

Verified after the change with the same import graph, `npx vitest run`, `npx eslint .` and `npm run build`.

- **Deleted, no importer anywhere (grep of `src` incl. tests, static and `import()` strings):** `AssistantRecordPicker.jsx`, `LivePulse.jsx` + `.module.css`, `PropertySpecs.jsx` + `.module.css`, `SubscriptionBadge.jsx` + `.module.css`, `motion/KineticText.jsx` + `.module.css` (its only user was `AgentStatusBar`), `motion/BorderBeam.module.css`.
- **Barrel deleted:** `components/index.js` is gone. `App.jsx` imports `CrmShell` / `LoginVault` and `main.jsx` imports `ErrorBoundary` directly. `DossierPanel` is now its own lazy chunk (loaded by Deals/EntitySheet), `LiveTranscript` rides `PowerDialerPage`'s chunk, `AgentStatusBar` rides `PersonalCommandComposer`'s. Entry chunk 454,549 → 432,456 bytes raw (142.4 → 136.1 KB gzip); initial download (entry + preloads + CSS) 579,001 → 520,644 bytes. The built entry no longer contains "ORCL", "AI Closer", "SCOUTING_MATRIX", "JARVIS", "Command Center" or "Whisper instruction".
- **`useJarvisVoice` removed** from `App.jsx`, with the reducer's dead `SET_JARVIS_LISTENING` / `SET_JARVIS_TRANSCRIPT` / `JARVIS_COMMAND` actions, `jarvisListening` / `jarvisTranscript` state and `applyJarvisCommand` (nothing dispatched `SET_JARVIS_LISTENING`; recognition never `start()`ed).
- **`AgentStatusBar`** — still LEGACY only because its host is. Renders nothing when idle; a running/failed/finished request shows icon + words (`role="status"`), no scramble, no gold beam.
- **`LiveTranscript`** — CONTEXTUAL (Work → … → Neoh · Sales · Power Dialer). Product copy, speaker codes mapped to words, offer guidance spelled out with an icon, `role="log" aria-live="off"`, tokenized CSS that works in light mode. A note is only logged once actually sent.
- **`motion/BorderBeam`** — kept as a no-op (`return null`) because `CrmShell` and `PersonalCommandComposer` (agent 3) still import it. Delete once those imports go.
- **`.hud-glass-panel` / `.hud-reticle`** (index.css) — neutralised: no gold hairline, no reticle pseudo-elements; layout-only.
- **`ErrorBoundary`** — labelled boundaries render inline in their section (were `position: fixed` full-screen); calm copy; raw error behind a collapsed "Technical details".
- **New:** `IntegrationStatus` (component) + `lib/integrationStatus.js` — the one customer vocabulary for integration state ("Phone · Ready", "Messaging · Needs verification", "MLS · Syncing", "Calendar · Reconnect"), icon + text. Used by `BrokerageSetupPanel`, `ProviderDeliveryPage`, `BrokerageOnboardingPanel`. Class: CONTEXTUAL primitive.

## 10. Owned by other agents: recommendations

| Owner | Item | Recommendation |
|---|---|---|
| Agent 3 | `OurAITab` (Neoh tab) | Replace it as the Neoh destination with the full-screen `NeohSurface` conversation (U7). Re-home Sales, Sites and Autonomy under Work or Settings. Remove the "Command" and "Intelligence" workspaces, which duplicate Home and `Work?type=opportunities`. Its "Closely-style … HomeGPT" copy (line ~538) names competitors. |
| Agent 3 | `CommandCenter` | Retire it. `NeohHome` is the home and `IntelligenceFeed` already has a Work view. |
| Agent 3 | `PersonalCommandComposer` | Retire it in favour of the single Neoh composer. If it stays, drop the `hud-glass-panel hud-reticle` classes and `BorderBeam` (lines 210, 254, 211, 260). Once it is gone, `AgentStatusBar` has no host and can be deleted. |
| Agent 3 | `PersonalAITab` double entry | Keep one entry (`Profile → AI controls`) and drop the `Neoh · Automations` mount. `BrokerageOnboardingPanel` and `CommandApprovalPanel` then each lose a duplicate mount. |
| Agent 3 | `neoh/MissionBuilder` | Give it a navigation entry (for example a Work "Automations" chip), or classify it as an internal or dev tool. Right now only a typed URL reaches it. |
| Agent 3 | `neoh/riveInputs.js`, `neoh/NeohAvatarRive.jsx` | `riveInputs.js` is imported only by `NeohAvatar.test.jsx`. The Rive path cannot succeed (no asset, no package). Keep both as a dormant contract or delete them together. |
| Agent 3 | `CrmShell` profile sheet | `hud-glass-panel hud-reticle` plus `BorderBeam` at lines 549 and 559. Agent 2 neutralizes the CSS classes in `index.css`; dropping `<BorderBeam>` is agent 3's call. |
| Agent 3 | `OurAITab` copy | Line 643 "One command center for NEOH’s…"; lines 381/442/559 "tenant-safe / tenant-scoped provider credentials", "Google, SMTP, and Twilio credentials…"; line 720 "Provider links"; line 724 "Actual backend and provider state"; "NEOH" all-caps in prose (75, 475, 645, 655, 723). Use "Neoh", "your brokerage", "connected accounts". |
| Agent 3 | `AssistantMessages` empty state | Line 47 promises "read a PDF or photo" but the Neoh composer has no attachment control — same false promise `ProductTour` made (fixed there). Also "NEOH" → "Neoh" (47, 67, 72). |
| Agent 3 | `neoh/useSpeechInput.js:12-13` | Comment still references the deleted `useJarvisVoice` / `applyJarvisCommand`. |
| Agent 3 | `AssistantShell.module.css` | Classes used only by the deleted `AssistantRecordPicker` can go. |
| Agent 5 | `CaptureSessionPanel.jsx` | "The reconstruction job failed." (71), "the job keeps running" (175) → "task"/"capture" language; route errors through `friendlyError` from `lib/errorMessages.js`. |
| Agent 5 | `PropertyTour.jsx:60-61` | "The 3D map service could not be reached / did not respond" → product language ("The 3D view couldn’t load. Try again in a moment."). |
| Agent 5 | `PropertyViewTab`, `RehabEditorDrawer` | Replace raw `err?.message` with `friendlyError(err, { fallback })`. |

