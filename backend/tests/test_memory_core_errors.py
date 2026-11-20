"""A database, sealing or configuration failure answers with a fixed sentence:
the exception text never reaches the browser.

What those handlers catch is RuntimeError from the pool or the crypto layer,
and its text names configuration ("ORACLE_ENCRYPTION_MASTER_KEY is not set",
the pool's state, the table that would not decrypt). Forty-one routes used to
send it as "Memory Core offline (<text>)"; three messaging routes sent it as
"Telnyx rejected ...: <text>"."""

from __future__ import annotations

import ast
import asyncio
import io
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from db.connection import DatabaseUnavailable
from memory_core_errors import memory_core_offline
from tenancy import Role, TenantContext

BACKEND = Path(__file__).resolve().parents[1]
SECRET_SHAPED = "ORACLE_ENCRYPTION_MASTER_KEY is not set for user neoh_app on table interaction_logs"
TENANT = "00000000-0000-0000-0000-0000000000a1"
AGENT = TenantContext(agent_id="agent@offline.test", tenant_id=TENANT, role=Role.AGENT)
OWNER = TenantContext(agent_id="owner@offline.test", tenant_id=TENANT, role=Role.BROKER_OWNER)


def _caught(exc: BaseException) -> BaseException:
    """`exc` as a handler sees it: raised and caught in this frame."""
    try:
        raise exc
    except BaseException as caught:  # noqa: BLE001 - the point is to hold it
        return caught


def test_the_detail_is_fixed_and_the_log_names_types_for_foreign_errors(caplog):
    try:
        try:
            raise OSError("pool host db.internal:25060 refused")
        except OSError as inner:
            raise RuntimeError(SECRET_SHAPED) from inner
    except RuntimeError as exc:
        with caplog.at_level(logging.ERROR, logger="oracle.memory_core"):
            plain = memory_core_offline(exc)
            saved = memory_core_offline(exc, "note not persisted")
    assert (plain.status_code, plain.detail) == (503, "Memory Core offline.")
    assert (saved.status_code, saved.detail) == (503, "Memory Core offline — note not persisted.")
    logged = caplog.text
    assert "in test_the_detail_is_fixed_and_the_log_names_types_for_foreign_errors" in logged, "the route"
    assert "RuntimeError caused by OSError" in logged
    assert "note not persisted" in logged
    for fragment in ("ORACLE_ENCRYPTION_MASTER_KEY", "neoh_app", "interaction_logs", "db.internal"):
        assert fragment not in logged, "a foreign RuntimeError and every cause are logged by type"


def test_app_authored_errors_log_their_reason_but_never_send_it(caplog):
    """DatabaseUnavailable.reason and SealError's table name are what an
    operator needs, and carry no row content; the browser still gets the
    fixed sentence."""
    from sealed_text import SealError

    with caplog.at_level(logging.ERROR, logger="oracle.memory_core"):
        pool = memory_core_offline(_caught(DatabaseUnavailable("pool_exhausted")))
        seal = memory_core_offline(_caught(SealError("interaction_logs text could not be decrypted")))
    assert pool.detail == seal.detail == "Memory Core offline."
    assert "DatabaseUnavailable: pool_exhausted" in caplog.text
    assert "SealError: interaction_logs text could not be decrypted" in caplog.text


def test_a_route_sends_no_exception_text(monkeypatch):
    """Through a real handler: the CRM touch history over a tenant_tx that
    raises the crypto layer's own message."""
    import crm

    @asynccontextmanager
    async def broken(_ctx):
        raise RuntimeError(SECRET_SHAPED)
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(crm, "tenant_tx", broken)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(crm.client_interactions("00000000-0000-0000-0000-0000000000c1", 50, AGENT))
    assert (exc.value.status_code, exc.value.detail) == (503, "Memory Core offline.")


def _registration_calls(messaging_api):
    return {
        "brand": lambda: messaging_api.register_brand(messaging_api.BrandRegister.model_construct(), OWNER),
        "campaign": lambda: messaging_api.register_campaign(
            messaging_api.CampaignRegister.model_construct(description="d", usecase="MIXED"), OWNER),
        "assign": lambda: messaging_api.assign_campaign_number(OWNER),
        "refresh": lambda: messaging_api.refresh_registration(OWNER),
    }


@pytest.mark.parametrize("route", ["brand", "campaign", "assign", "refresh"])
def test_a_misconfigured_texting_provider_names_no_setting(monkeypatch, route):
    """The provider is resolved before anything else; its error names the
    server's environment variable and value, which no broker owner can set."""
    import messaging_api

    async def number(_ctx):
        return {"voice_caller_id_e164": "+13025550100"}

    monkeypatch.setattr(messaging_api, "get_public_business_number", number)
    monkeypatch.setenv("ORACLE_MESSAGING_PROVIDER", "carrier-pigeon")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_registration_calls(messaging_api)[route]())
    assert (exc.value.status_code, exc.value.detail) == (503, "Texting is not configured on this server.")


@pytest.mark.parametrize("route,target,outcome", [
    ("brand", "register_messaging_brand", "brand registration not recorded"),
    ("campaign", "register_messaging_campaign", "campaign submission not recorded"),
    ("assign", "assign_messaging_campaign_number", "number assignment not recorded"),
])
def test_registration_separates_a_provider_refusal_from_a_database_failure(monkeypatch, route, target, outcome):
    """The provider's own refusal is shown to the owner fixing a registration;
    a database failure under it is not a refusal and does not say one."""
    import messaging_api
    from command_providers import ProviderRejectedError

    async def creds(_ctx):
        return {}

    async def number(_ctx):
        return {"voice_caller_id_e164": "+13025550100"}

    monkeypatch.setenv("ORACLE_MESSAGING_PROVIDER", "telnyx")
    monkeypatch.setattr(messaging_api, "_telnyx_credentials", creds)
    monkeypatch.setattr(messaging_api, "get_public_business_number", number)
    calls = _registration_calls(messaging_api)

    async def refused(*_a, **_k):
        raise ProviderRejectedError("10DLC brand EIN does not match the legal name")

    monkeypatch.setattr(messaging_api, target, refused)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(calls[route]())
    assert exc.value.status_code == 502
    assert exc.value.detail.startswith("The texting provider refused the ")
    assert exc.value.detail.endswith(": 10DLC brand EIN does not match the legal name")

    async def database_down(*_a, **_k):
        raise DatabaseUnavailable("DB pool not initialized — call init_pool() at startup.")

    monkeypatch.setattr(messaging_api, target, database_down)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(calls[route]())
    assert (exc.value.status_code, exc.value.detail) == (503, f"Memory Core offline — {outcome}.")


def test_the_admin_read_logs_a_database_error_by_type(monkeypatch, caplog):
    """A database error's text can quote the value it rejected."""
    import admin_ops

    class _Rejected(Exception):
        pass

    @asynccontextmanager
    async def rejecting(_ctx):
        raise _Rejected('invalid input syntax for type uuid: "jane@client.test"')
        yield  # pragma: no cover

    monkeypatch.setattr(admin_ops, "tenant_tx", rejecting)
    with caplog.at_level(logging.ERROR), pytest.raises(HTTPException) as exc:
        asyncio.run(admin_ops._fetch(AGENT, "SELECT 1"))
    assert (exc.value.status_code, exc.value.detail) == (503, "Memory Core offline.")
    assert "_Rejected" in caplog.text and "jane@client.test" not in caplog.text


def test_floorplan_extraction_unavailable_sends_no_library_text(monkeypatch):
    import floorplan_api
    import floorplan_pipeline

    @asynccontextmanager
    async def tx(_ctx):
        yield object()

    async def exists(*_a):
        return None

    def no_opencv(*_a, **_k):
        raise RuntimeError("cv2 failed to import from /opt/venv/lib/python3.12/site-packages")

    monkeypatch.setattr(floorplan_api, "tenant_tx", tx)
    monkeypatch.setattr(floorplan_api, "_assert_subject_exists", exists)
    monkeypatch.setattr(floorplan_pipeline, "extract_from_floorplan_image", no_opencv)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 64
    with pytest.raises(HTTPException) as exc:
        asyncio.run(floorplan_api.extract_image(
            file=UploadFile(io.BytesIO(png), filename="plan.png"), metres_per_pixel=0.01,
            known_total_sqft=None, wall_height_m=2.5, lead_id=uuid.uuid4(), listing_id=None, ctx=AGENT))
    assert (exc.value.status_code, exc.value.detail) == (503, "Floor-plan extraction is not available on this server.")


# ── the gate ────────────────────────────────────────────────────────────────

#: Handlers that catch these can be holding the database, sealing or crypto
#: layer's error, whatever the try body looks like today.
_BROAD = {"RuntimeError", "Exception", "BaseException", "CryptoError", "SealError",
          "NoteSealError", "DatabaseUnavailable"}

#: Reviewed: the exception's text IS the message meant for the reader.
_ALLOWED = {
    "ai_chat_api.py::upload_attachments": "ClamAV wrapper raises fixed sentences only",
    "data_sources_api.py::regrid_parcel": "RegridAuthError tells the operator to rotate the token",
    "messaging_api.py::upload_hosted_document": "the try wraps only the Telnyx document upload",
    "sales_api.py::send_test_email": "the tenant's own SMTP server's answer, for the tenant",
}


def _type_names(node) -> set[str]:
    if node is None:
        return {"<bare>"}
    if isinstance(node, ast.Tuple):
        return set().union(*(_type_names(e) for e in node.elts))
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    return set()


def _http_details_quoting_the_exception(tree: ast.AST):
    """(function, line) for each HTTPException built in a broad handler whose
    detail (any argument but the status) reads the caught name: str(exc),
    f"{exc}", "%s" % exc, "..." + str(exc), exc.args, across lines too."""
    owner: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(fn):
                owner[id(n)] = fn.name   # innermost wins: walk order is outer first
    for handler in ast.walk(tree):
        if not (isinstance(handler, ast.ExceptHandler) and handler.name and _type_names(handler.type) & _BROAD):
            continue
        for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
            func = getattr(node, "func", None)
            if not (isinstance(node, ast.Call) and (
                    (isinstance(func, ast.Name) and func.id == "HTTPException")
                    or (isinstance(func, ast.Attribute) and func.attr == "HTTPException"))):
                continue
            detail = list(node.args[1:]) + [k.value for k in node.keywords if k.arg != "status_code"]
            if any(isinstance(x, ast.Name) and x.id == handler.name for part in detail for x in ast.walk(part)):
                yield owner.get(id(handler), "<module>"), node.lineno


def test_the_gate_sees_every_spelling():
    src = '''
async def route():
    try:
        pass
    except RuntimeError as exc:
        raise HTTPException(503, f"Memory Core offline ({exc})")
    except Exception as err:
        raise HTTPException(status_code=503, detail="offline (%s)" % err)
    except SealError as e:
        raise HTTPException(503,
                            "note not saved "
                            + str(e))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
'''
    assert [line for _, line in _http_details_quoting_the_exception(ast.parse(src))] == [6, 8, 10]


def test_no_route_sends_an_infrastructure_error_text():
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if any(part in ("venv", "node_modules", "pascal-editor", "tests") for part in path.parts):
            continue
        rel = str(path.relative_to(BACKEND))
        for function, line in _http_details_quoting_the_exception(ast.parse(path.read_text(errors="ignore"))):
            if f"{rel}::{function}" not in _ALLOWED:
                offenders.append(f"{rel}:{line} ({function})")
    assert not offenders, offenders
