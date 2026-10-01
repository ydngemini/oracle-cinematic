"""Cross-tenant BOLA/IDOR sweep against a Neoh test topology (never production).

Signs in as an owner and an agent of brokerage A and of brokerage B (synthetic
perf-* tenants from fixtures/seed.py), harvests B's object identifiers using B's
own session, then replays every identifier-bearing route in the OpenAPI schema
with A's sessions and B's identifiers.

A response is a LEAK when it carries B's sentinel (seed.py plants one in every
tenant's records) or echoes an identifier that only B's harvest contained.
A 2xx on a mutating method with a B identifier is a SUSPECT for manual triage.

Run inside the topology network (the target must report a loadtest/staging
environment — guarded below):

  docker exec -i -e NEOH_SECURITY_TEST_ALLOWED=1 oracle-perf-worker \
      python /perf/security/idor_sweep.py > out.json
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
import time
from collections import defaultdict
from urllib.parse import quote

import httpx

BASE = os.getenv("TARGET", "http://oracle-perf-lb:8080")
USERS = os.getenv("USERS_FILE", "/perf/out/users.json")
TENANT_A = os.getenv("TENANT_A", "perf-brokerage-01")
TENANT_B = os.getenv("TENANT_B", "perf-brokerage-02")
MUTATE = os.getenv("MUTATE", "1") == "1"

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# Never replay these with a foreign identifier: they end the attacker's own
# session or are not tenant-object routes.
SKIP_PREFIXES = ("/auth/logout", "/ws", "/health", "/version", "/docs", "/redoc", "/openapi")


def guard() -> None:
    if os.getenv("NEOH_SECURITY_TEST_ALLOWED") != "1":
        sys.exit("refusing: set NEOH_SECURITY_TEST_ALLOWED=1 (test topology only)")
    env = httpx.get(f"{BASE}/version", timeout=10).json().get("environment", "")
    if env not in ("loadtest", "staging", "development", "test"):
        sys.exit(f"refusing: target reports environment={env!r}")


class Session:
    def __init__(self, user: dict, ip: str):
        self.user = user
        self.c = httpx.Client(base_url=BASE, timeout=30, headers={"X-Forwarded-For": ip})
        r = self.c.post("/auth/login", json={"agent_id": user["agent_id"], "passphrase": user["password"]})
        r.raise_for_status()
        self.token = r.cookies.get("oracle_session")
        p = self.c.get("/auth/policy-acceptance", cookies={"oracle_session": self.token})
        self.csrf = p.cookies.get("csrf_token")

    def req(self, method: str, path: str, body=None) -> httpx.Response:
        cookies = {"oracle_session": self.token}
        headers = {}
        if method != "GET" and self.csrf:
            cookies["csrf_token"] = self.csrf
            headers["X-CSRF-Token"] = self.csrf
        for attempt in range(4):
            r = self.c.request(method, path, json=body, cookies=cookies, headers=headers)
            if r.status_code != 429:
                return r
            time.sleep(float(r.headers.get("retry-after", "2") or 2))
        return r


def walk_ids(obj, key_hint: str, out: dict) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (str, int)) and not isinstance(v, bool) and (k == "id" or k.endswith("_id") or k.endswith("Id")):
                sv = str(v)
                if sv and len(sv) < 200:
                    out[k if k != "id" else f"id@{key_hint}"].add(sv)
            else:
                walk_ids(v, k if isinstance(v, (dict, list)) else key_hint, out)
    elif isinstance(obj, list):
        for v in obj[:200]:
            walk_ids(v, key_hint, out)


def harvest(s: Session, paths: dict) -> dict:
    ids: dict = defaultdict(set)
    for path, ops in paths.items():
        if "{" in path or "get" not in ops or path.startswith(SKIP_PREFIXES):
            continue
        required = [p for p in ops["get"].get("parameters", []) if p.get("required")]
        if required:
            continue
        r = s.req("GET", path)
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            seg = [x for x in path.split("/") if x][-1]
            try:
                walk_ids(r.json(), seg, ids)
            except ValueError:
                pass
    return ids


def candidates(path: str, param: str, b_only: dict, b_user: dict) -> list[str]:
    segs = [x for x in path.split("/") if x]
    try:
        prev = segs[segs.index("{" + param + "}") - 1]
    except (ValueError, IndexError):
        prev = ""
    stem = prev.rstrip("s")
    pick: list[str] = []
    for key, vals in b_only.items():
        k = key.lower()
        if k == param.lower() or (key.startswith("id@") and prev and key[3:] in (prev, stem, prev + "s")) \
           or (stem and k.startswith(stem) and k.endswith("_id")):
            pick.extend(sorted(vals)[:3])
    if "tenant" in param:
        pick.append(b_user["tenant_id"])
    if "agent" in param or "user" in param or "member" in param:
        pick.append(b_user["agent_id"])
    if not pick:  # unknown shape: try a spread of B-only UUIDs
        uu = sorted({v for vals in b_only.values() for v in vals if UUID_RE.match(v)})
        pick = uu[:4]
    return list(dict.fromkeys(pick))[:6]


def synth(schema: dict, spec: dict, depth: int = 0):
    """A minimal schema-valid value, so a mutation reaches authorization instead
    of failing validation (a 422 proves nothing about access control)."""
    if depth > 6 or not isinstance(schema, dict):
        return None
    if "$ref" in schema:
        node = spec
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node.get(part, {})
        return synth(node, spec, depth + 1)
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            options = [o for o in schema[key] if o.get("type") != "null"] or schema[key]
            return synth(options[0], spec, depth + 1)
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    if "default" in schema and schema["default"] is not None:
        return schema["default"]
    t = schema.get("type")
    fmt = schema.get("format", "")
    if t == "object" or "properties" in schema:
        props = schema.get("properties", {})
        return {k: synth(props[k], spec, depth + 1) for k in schema.get("required", []) if k in props}
    if t == "array":
        n = max(1, schema.get("minItems", 0))
        return [synth(schema.get("items", {}), spec, depth + 1) for _ in range(n)]
    if t == "integer":
        return max(1, schema.get("minimum", 1))
    if t == "number":
        return float(max(1, schema.get("minimum", 1)))
    if t == "boolean":
        return False
    if fmt == "uuid":
        return str(uuid.uuid4())
    if fmt in ("date-time",):
        return "2030-01-01T10:00:00+00:00"
    if fmt == "date":
        return "2030-01-01"
    if fmt == "email" or "email" in str(schema.get("title", "")).lower():
        return "secreview@example.test"
    if "pattern" in schema and "\\+" in schema["pattern"]:
        return "+13025550123"
    min_len = schema.get("minLength", 1)
    return "secreview-" + "x" * max(0, min_len - 10) if min_len > 10 else "secreview"


def body_for(op: dict, spec: dict):
    content = (op.get("requestBody") or {}).get("content", {})
    schema = (content.get("application/json") or {}).get("schema")
    return synth(schema, spec) if schema else None


def main() -> None:
    guard()
    users = json.load(open(USERS))["users"]
    def pick(slug, role):
        return next(u for u in users if u["tenant_slug"] == slug and u["role"] == role)
    a_owner, a_agent = pick(TENANT_A, "broker_owner"), pick(TENANT_A, "agent")
    b_owner, b_agent = pick(TENANT_B, "broker_owner"), pick(TENANT_B, "agent")
    sA_owner, sA_agent = Session(a_owner, "203.0.113.11"), Session(a_agent, "203.0.113.12")
    sB_owner = Session(b_owner, "203.0.113.21")
    Session(b_agent, "203.0.113.22")

    spec = sA_owner.req("GET", "/openapi.json").json()
    paths = spec["paths"]
    b_ids = harvest(sB_owner, paths)
    a_ids = harvest(sA_owner, paths)
    a_vals = {v for vals in a_ids.values() for v in vals}
    b_only = {k: {v for v in vals if v not in a_vals} for k, vals in b_ids.items()}
    b_only = {k: v for k, v in b_only.items() if v}
    b_only_vals = {v for vals in b_only.values() for v in vals if len(v) >= 8}

    results = []
    for path, ops in paths.items():
        if "{" not in path or path.startswith(SKIP_PREFIXES):
            continue
        params = re.findall(r"{([^}]+)}", path)
        for method in ops:
            m = method.upper()
            if m not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                continue
            if m != "GET" and not MUTATE:
                continue
            combos = []
            for p in params:
                combos.append(candidates(path, p, b_only, b_owner))
            # one substitution per param position, others filled with the first candidate
            for i, cands in enumerate(combos):
                for val in cands:
                    filled = path
                    for j, p in enumerate(params):
                        v = val if j == i else (combos[j][0] if combos[j] else "x")
                        filled = filled.replace("{" + p + "}", quote(str(v), safe=""), 1)
                    for who, s in (("A_agent", sA_agent), ("A_owner", sA_owner)):
                        body = (body_for(ops[method], spec) or {}) if m in ("POST", "PUT", "PATCH") else None
                        r = s.req(m, filled, body)
                        text = r.text[:200000]
                        leak_sentinel = b_owner["sentinel"] in text
                        echoed = [v for v in b_only_vals if v in text and v != val][:3]
                        verdict = "LEAK" if (leak_sentinel or (r.status_code < 300 and echoed)) else (
                            "SUSPECT" if r.status_code < 300 else "ok")
                        results.append({
                            "route": f"{m} {path}", "url": filled, "as": who, "status": r.status_code,
                            "verdict": verdict, "sentinel": leak_sentinel, "echoed_b_ids": echoed,
                            "detail": text[:160] if verdict != "ok" else "",
                        })
    summary = defaultdict(int)
    for x in results:
        summary[x["verdict"]] += 1
    json.dump({
        "target": BASE, "tenant_a": TENANT_A, "tenant_b": TENANT_B,
        "b_id_kinds": {k: len(v) for k, v in b_only.items()},
        "requests": len(results), "summary": summary,
        "flagged": [x for x in results if x["verdict"] != "ok"],
        # How far mutations got: 401/403/404 = refused by authorization or RLS;
        # 422 = the synthesized body still failed validation (inconclusive).
        "server_errors": [{"route": x["route"], "url": x["url"], "as": x["as"]}
                          for x in results if x["status"] >= 500],
        "mutation_status": dict(sorted(
            {str(st): sum(1 for x in results if not x["route"].startswith("GET") and x["status"] == st)
             for st in {x["status"] for x in results}}.items())),
    }, sys.stdout, indent=1, default=list)


if __name__ == "__main__":
    main()
