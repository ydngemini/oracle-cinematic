-- ---------------------------------------------------------------------------
-- 0137 — a client's linked contact moves with the client (owner, 2026-10-13).
--
-- 0131 scoped clients and 0134 scoped agent_contacts per agent, but the two
-- assignments were independent: reassigning a client left its contact with
-- the previous agent, so the new agent saw the client and not the contact
-- (and the old agent the reverse). The owner chose "move together".
--
-- After a client's assignee changes, its linked contact (clients.contact_id,
-- or agent_contacts.legacy_client_id) takes the same assignee in the same
-- transaction. The move goes through 0134's guard (app_may_reassign) and the
-- caller's own RLS, and skips — never fails — a contact the caller may not
-- move, or a new assignee who is no longer active (the contacts trigger
-- requires an active member). A skipped contact stays as it was; the 0134
-- mismatch count still reports it. A client reassignment therefore never
-- fails because of its contact.
--
-- Rollback: DROP TRIGGER IF EXISTS trg_clients_carry_contact_assignee ON
-- clients; DROP FUNCTION IF EXISTS clients_carry_contact_assignee();
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION clients_carry_contact_assignee() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public AS
$$
BEGIN
    IF NEW.assignee_user_id IS NOT DISTINCT FROM OLD.assignee_user_id THEN
        RETURN NULL;
    END IF;
    IF NEW.assignee_user_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.users u WHERE u.id = NEW.assignee_user_id AND u.is_active) THEN
        RETURN NULL;
    END IF;
    UPDATE public.agent_contacts ac
       SET assigned_agent_id = NEW.assignee_id,
           updated_at = now()
     WHERE ac.tenant_id = NEW.tenant_id
       AND ac.deleted_at IS NULL
       AND (ac.id = NEW.contact_id OR ac.legacy_client_id = NEW.id)
       AND ac.assigned_user_id IS DISTINCT FROM NEW.assignee_user_id
       AND app_may_reassign(ac.assigned_user_id, NEW.assignee_user_id);
    RETURN NULL;
END
$$;
REVOKE ALL ON FUNCTION clients_carry_contact_assignee() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_clients_carry_contact_assignee ON clients;
CREATE TRIGGER trg_clients_carry_contact_assignee
    AFTER UPDATE OF assignee_id ON clients
    FOR EACH ROW EXECUTE FUNCTION clients_carry_contact_assignee();
