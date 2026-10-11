# Neho AI Real Estate Operating System — Shared Project Notes
**Date:** October 11, 2026  
**Repository:** `ydngemini/oracle-cinematic`

These notes summarize the recent project discussion. They capture the goals and plans discussed; they do not claim that planned features are already implemented.

## 1. Product name and positioning
- Project name: **Neho AI Real Estate Operating System**.
- Do not position the product as only a CRM. The intended scope goes beyond contact management into AI communications, follow-up and relationship nurturing, marketing, content tools, neighborhood updates, and 3D property experiences.
- Target customers are **whole real estate brokerages**, rather than individual agents one at a time.
- The product should be designed with brokerage owners and teams in mind, including agent workflows and permissions.

## 2. Business model and pricing discussion
- The business goal is to sell one brokerage-wide service agreement rather than rely mainly on many individual-agent sales.
- A preliminary pricing idea discussed was approximately **$1,999 per month for a 20-agent brokerage**, with a possible lower introductory pilot around **$1,499 per month** and higher pricing only when advanced capabilities and costs justify it.
- Earlier tier ideas included around $999 for smaller brokerages and custom enterprise pricing.
- These are hypotheses to validate, not final prices. Validate willingness to pay, onboarding/support costs, AI call and messaging usage, hosting, and the value demonstrated to brokerages before setting final tiers.
- Explain the return on investment in terms of agent time saved, better follow-up, client retention, and brokerage productivity.

## 3. MLS scope
- **MLS integration is currently out of scope.** The project owner says the product does not have an MLS connection and cannot currently afford or obtain the necessary approval.
- Do not represent MLS access, MLS search, or MLS data integration as an existing or planned included feature unless the project owner changes this decision.
- Any property information or property-search capability must be designed around data sources the product is actually authorized to use.

## 4. Planned development sequence
The intended order is:
1. Finish the AI phone-call workflow.
2. Add SMS capabilities.
3. Add email capabilities.
4. Add follow-up workflows.
5. Build long-term client relationship nurturing.

The owner wants to build the major pieces before beginning comprehensive end-to-end testing across the product, including 3D property design and AI agents. Individual components should still be developed carefully, and the complete integrated workflows should be tested before being treated as production-ready.

## 5. AI approval and control principle
- AI may analyze information and recommend an action, but **the real estate agent must remain in control**.
- The intended manual-approval workflow is: AI prepares a suggestion or draft → agent reviews it → agent approves or rejects it → only an approved action may execute.
- If the agent rejects or does not approve an action, the AI must not carry it out.
- Provide manual and automatic modes only where deliberately configured. The default for consequential client-facing actions should be explicit approval.
- Enforce approval in the backend/application, not merely in the model's prompt.
- Bind approval to the exact content/action approved. If a draft changes after approval, require approval again.
- Record approvals, rejections, execution outcomes, and relevant audit information. Respect agent permissions and client communication preferences/opt-outs.

## 6. AI phone calls and CRM notes
- Recent repository work was previously observed around live AI phone-conversation progress, CRM call notes, and a fix relating to SMTP credential security.
- The phone AI connecting to CRM notes/context is an important foundation for later follow-up and relationship features.
- A capability being discussed or code being present does not by itself prove that a workflow is fully implemented or production-ready. Verify behavior through code review and testing.

## 7. Long-term client relationship agent
The product vision is to help agents maintain useful, human-feeling relationships with clients over many years—potentially 10–20 years—without feeling intrusive or spammy.

Potential capabilities:
- Keep relevant notes from authorized conversations.
- Remember client-stated preferences, important milestones, property history, and follow-up commitments.
- Send suggested check-ins such as “How is the home working out for you?”.
- Suggest a home-purchase or move-in anniversary message.
- Offer relevant neighborhood or market updates when the source is authorized and the information is useful.
- Notice possible future needs from actual, relevant signals and suggest them to the agent for review.
- Update or expire information when it becomes outdated; distinguish facts the client explicitly stated from AI inferences.
- Use consent, appropriate frequency limits, privacy protections, and opt-out handling.

Example: if records show a client has lived in a home for several years, the AI might suggest a thoughtful check-in to the agent. It should not assume that the client wants to move just because time has passed or because their children may have grown.

## 8. Personalized AI for each agent and client
- The owner wants the AI to adapt to an agent's communication habits and preferences, and to consider how clients respond and interact.
- Use observed behavior as context for **tentative recommendations**, not as certain claims about a person's feelings, intentions, or future plans.
- Explain the evidence behind recommendations and indicate uncertainty.
- Agent approval is required before the AI takes the proposed client-facing action.

## 9. Email AI: intended capabilities
The email agent should help an agent understand and respond to client emails in context. Intended abilities:
1. Read incoming email and relevant thread history.
2. Identify explicit questions, requests, concerns, preferences, and commitments.
3. Retrieve relevant, authorized CRM notes and previous communications.
4. Distinguish confirmed facts from assumptions and uncertain interpretations.
5. Draft a natural, situation-appropriate response.
6. Match the individual agent's preferred tone, formality, length, greeting, and sign-off.
7. Explain briefly why the proposed response fits the situation.
8. Check for accuracy, relevance, tone, unanswered questions, and unsupported claims.
9. Read the draft aloud.
10. Revise the draft from the agent's spoken or written feedback.
11. Require explicit approval of the final version before sending.

Never invent property facts, client preferences, promises, or completed actions. If the system cannot verify whether something was done, ask the agent to check rather than guessing.

## 10. Email AI: recommended implementation/training approach
A new model does not need to be trained from scratch to begin. Start with a capable existing language model, clear instructions, relevant context retrieval, carefully reviewed examples, and systematic evaluation.

Recommended sequence:
1. Implement email ingestion and thread history.
2. Extract structured information: client questions, stated concerns, preferences, and outstanding commitments.
3. Retrieve only relevant, authorized CRM context.
4. Draft replies grounded in that context.
5. Create a per-agent communication profile.
6. Collect agent edits and feedback.
7. Evaluate drafts against a repeatable test set.
8. Add text-to-speech/read-aloud and voice-based revisions.
9. Enforce approval technically before sending.
10. Test with real estate professionals using authorized data; improve based on observed failures.

Prompting and retrieval-augmented generation (RAG) are the recommended starting point. Consider fine-tuning only later if evaluations show persistent weaknesses that fine-tuning is likely to address. Do not use model training as the memory store for changing client details; retrieve those details from authorized records.

## 11. Email AI evaluation checklist
Test whether the agent:
- Correctly identifies the client's actual question or concern.
- Uses relevant history and does not invent facts.
- Matches the agent's configured writing style.
- Directly answers the question and handles every important point.
- Uses an appropriate tone without pressuring the client.
- Clearly distinguishes facts from uncertain interpretations.
- Flags missing or sensitive information when needed.
- Cannot send without valid approval from the authorized agent.
- Requires renewed approval if the email changes after approval.
- Records sending results and errors accurately.

When an error occurs, preserve a privacy-appropriate test case, identify the root cause, update prompts/retrieval/logic as appropriate, and rerun the regression tests.

## 12. 3D property design from pictures
- **3D property work created from pictures is a core differentiator in the owner's vision**, not something to forget or automatically defer as a minor add-on.
- The owner wants this capability included in the same overall brokerage product as the AI communications and marketing tools.
- Competitor differentiation should explicitly evaluate this feature, but claims that competitors lack it should be verified before being used in marketing.
- Include the 3D workflow in the planned end-to-end testing once the feature is available.

## 13. Marketing and content capabilities in the broader vision
The intended platform may also include:
- Social media marketing tools.
- Video transcription.
- A future AI-assisted video editing capability.
- AI-generated or assisted content for real estate marketing.
- Neighborhood updates based on current, authorized information sources.
- Brokerage-wide workflows and productivity features.

Treat future ideas as roadmap items until their implementation and behavior have been confirmed.

## 14. Competitor comparison and differentiation
Competitors discussed previously included Lofty, Follow Up Boss, and BoldTrail. The goal is to compare the capabilities relevant to Neho's actual intended scope—not to claim complete feature parity.

Important areas to compare include:
- AI phone calls and CRM call notes.
- SMS, email, and follow-up workflows.
- Long-term relationship nurturing.
- Agent approval and control of AI actions.
- Brokerage-level administration and team workflows.
- Social media marketing, video transcription, and planned video editing.
- 3D property experiences generated from pictures.
- Neighborhood updates.
- Integrations and operational reliability.

MLS integration is excluded from the current scope. For each competitor and Neho feature, distinguish **implemented and verified**, **partially implemented**, **planned**, and **not available**. Do not present an idea or roadmap item as a working feature.

## 15. Current implementation status: be precise
In the recent repository review, changes were observed relating to phone AI integration, CRM call notes, and a security fix involving SMTP credentials. Earlier AI memory work was also identified. Documentation for a 20-agent brokerage plan was observed, but documentation is not proof that all described features are built.

The complete 10–20-year relationship system, complete end-to-end workflows, and production readiness were **not confirmed** by that review. Continue to separate:
- Verified code changes.
- Features that run successfully in tests.
- Features that have passed end-to-end testing.
- Planned capabilities.

## 16. Overall product principle
Neho should aim to be a brokerage-wide AI operating system that brings communication, relationship management, marketing/content tools, and 3D property experiences together. The differentiator is not simply adding AI everywhere: it is making AI context-aware and useful while keeping the real estate professional in control of external actions.

**Priority:** build the planned capabilities in sequence, preserve the agent-approval principle throughout the system, and prove reliability with repeatable end-to-end tests before making production or marketing claims.


## 17. Cloud Notes and shared brokerage workspace
The recent discussion of “cloud notes” is documented here as a shared, cloud-accessible notes capability for Neho, connected to both team collaboration and client relationship memory. This is a product requirement/idea, not a claim that the feature is already implemented.

### Shared team notes
- Let authorized agents access relevant shared notes from the brokerage workspace, so information is not trapped on one agent's device.
- Support sharing relevant leads, client notes, tasks, calendars, updates, task assignments, progress tracking, and lead handoffs.
- Preserve useful client context when another authorized team member takes over a lead or task.
- Keep notes associated with the appropriate client/lead and allow agents to add dated updates rather than silently overwriting history.

### AI-generated client notes
- After authorized calls, emails, SMS conversations, or meetings, the AI may propose a structured note summarizing what the client actually said, preferences, questions, commitments, and agreed next steps.
- Keep confirmed statements separate from AI interpretations or possible future needs.
- Let the responsible agent review, correct, approve, or reject proposed notes according to the configured workflow.
- Approved notes can support later email drafts, follow-up suggestions, and long-term relationship nurturing.
- Examples of useful context include a preferred contact time, stated property preferences, a promised follow-up, or a move-in anniversary. Avoid collecting unnecessary sensitive personal details.

### Cloud access, privacy, and reliability
- Store notes in a secure cloud-backed service so authorized team members can access them across devices.
- Enforce brokerage, role, and client-level permissions on the server; one brokerage must not be able to access another brokerage's notes.
- Use encryption in transit and at rest, audit access and changes, and provide appropriate retention, export, correction, and deletion controls.
- Do not treat cloud storage as permission to share every note with every agent. Restrict access to people who need it for their role.
- Handle offline/network failures and sync conflicts clearly, and avoid losing edits.
- Respect client consent and applicable privacy requirements. Never let the AI send a client-facing message merely because it has created or updated a note; the separate agent-approval rule still applies.

### Suggested workflow
1. An authorized conversation or interaction occurs.
2. Neho drafts a proposed note and identifies its source.
3. The agent reviews and approves/corrects it where required.
4. The approved note is saved to the client's secure cloud record.
5. Authorized teammates can see relevant information according to permissions.
6. The relationship agent may use that information to recommend a future action.
7. The agent separately approves or rejects the proposed action.

### Implementation checks
- Verify that notes save and load across devices and sessions.
- Test role-based access and cross-brokerage isolation.
- Test note edits, history, sync failures, and recovery.
- Verify AI-generated notes are grounded in the actual conversation and flag uncertainty.
- Verify that rejection or lack of approval prevents a proposed note/action from being executed where approval is required.
