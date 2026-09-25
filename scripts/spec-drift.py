#!/usr/bin/env python3
"""What would this deploy change about the running app — and what would it DELETE?

`doctl apps update --spec` replaces the whole app spec. Anything someone added
through the DigitalOcean console since the last deploy — an env var, a
component, an ingress rule, a changed instance count — is not in the repo's
spec, so the next deploy silently removes it. Nobody is told; the app just
loses a setting, and the failure shows up later somewhere unrelated.

This compares the spec about to be applied with the one currently running and
reports, by NAME only (never a value — the running spec carries encrypted
secrets and plaintext config alike):

  REMOVALS   present in the running app, absent from the new spec.
             These fail the deploy unless ALLOW_SPEC_REMOVALS=1, because each is
             either an intended removal (say so) or a console edit about to be
             destroyed (put it in app.yaml first).

  CHANGES    the same thing with a different shape: process role, instance
             count, image repository, database and Valkey bindings, storage
             backend, ingress rules. Reported, not fatal — changing these is
             what deploys are for.

  ADDITIONS  new in this spec. Reported.

Image DIGESTS are expected to change every release and are not drift.

Usage:
  scripts/spec-drift.py --running running.yaml --new app.production.yaml
  exit 0 = no removals (or allowed), 1 = removals, 2 = unreadable input
"""

from __future__ import annotations

import argparse
import os
import sys

import yaml

KINDS = ("services", "workers", "static_sites", "jobs")


def _components(spec: dict) -> dict[str, tuple[str, dict]]:
    out = {}
    for kind in KINDS:
        for comp in spec.get(kind) or []:
            out[comp.get("name")] = (kind, comp)
    return out


def _env_names(comp: dict) -> set[str]:
    return {e.get("key") for e in comp.get("envs") or [] if e.get("key")}


def _env_value(comp: dict, key: str):
    for e in comp.get("envs") or []:
        if e.get("key") == key:
            return e.get("value")
    return None


def _ingress(spec: dict) -> set[str]:
    rules = (spec.get("ingress") or {}).get("rules") or []
    out = set()
    for r in rules:
        prefix = ((r.get("match") or {}).get("path") or {}).get("prefix")
        target = (r.get("component") or {}).get("name")
        out.add(f"{prefix} -> {target}")
    return out


def _bindings(spec: dict) -> dict[str, str]:
    return {db.get("name"): f"{db.get('engine')}:{db.get('cluster_name')}"
            for db in spec.get("databases") or []}


# Env values that describe SHAPE rather than secrets, safe to name in a report
# as "changed" (still never printed).
_SHAPE_ENVS = ("ORACLE_PROCESS_ROLE", "ORACLE_ENV", "ORACLE_S3_BUCKET",
               "ORACLE_S3_ENDPOINT_URL", "OBJECT_STORAGE_BACKEND", "ORACLE_OBJECT_STORAGE")


def diff(running: dict, new: dict) -> dict[str, list[str]]:
    removals, changes, additions = [], [], []

    rc, nc = _components(running), _components(new)
    for name in sorted(set(rc) - set(nc)):
        removals.append(f"component '{name}' ({rc[name][0]})")
    for name in sorted(set(nc) - set(rc)):
        additions.append(f"component '{name}' ({nc[name][0]})")

    for name in sorted(set(rc) & set(nc)):
        (rk, r), (nk, n) = rc[name], nc[name]
        if rk != nk:
            changes.append(f"'{name}' changes kind {rk} -> {nk}")
        for key in sorted(_env_names(r) - _env_names(n)):
            removals.append(f"env var {key} on '{name}'")
        for key in sorted(_env_names(n) - _env_names(r)):
            additions.append(f"env var {key} on '{name}'")
        for key in _SHAPE_ENVS:
            if key in _env_names(r) and key in _env_names(n) and _env_value(r, key) != _env_value(n, key):
                changes.append(f"{key} changes on '{name}'")
        if r.get("instance_count") != n.get("instance_count"):
            changes.append(f"instance_count on '{name}': {r.get('instance_count')} -> {n.get('instance_count')}")
        if r.get("instance_size_slug") != n.get("instance_size_slug"):
            changes.append(f"instance size on '{name}': {r.get('instance_size_slug')} -> {n.get('instance_size_slug')}")
        ri, ni = r.get("image") or {}, n.get("image") or {}
        if ri.get("repository") != ni.get("repository"):
            changes.append(f"image repository on '{name}': {ri.get('repository')} -> {ni.get('repository')}")
        if bool(ri) != bool(ni):
            changes.append(f"'{name}' source changes between image and build-from-source")

    rb, nb = _bindings(running), _bindings(new)
    for name in sorted(set(rb) - set(nb)):
        removals.append(f"database binding '{name}'")
    for name in sorted(set(rb) & set(nb)):
        if rb[name] != nb[name]:
            changes.append(f"database binding '{name}': {rb[name]} -> {nb[name]}")
    for name in sorted(set(nb) - set(rb)):
        additions.append(f"database binding '{name}'")

    ri, ni = _ingress(running), _ingress(new)
    for rule in sorted(ri - ni):
        removals.append(f"ingress rule {rule}")
    for rule in sorted(ni - ri):
        additions.append(f"ingress rule {rule}")

    rd = {d.get("domain") for d in running.get("domains") or []} - {None}
    nd = {d.get("domain") for d in new.get("domains") or []} - {None}
    # Domains are attached through the console today and absent from the spec.
    # Applying a spec without them would detach them — the worst removal there
    # is, because the public URL every provider calls stops resolving to us.
    for domain in sorted(rd - nd):
        removals.append(f"domain {domain}")

    return {"removals": removals, "changes": changes, "additions": additions}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--running", required=True)
    ap.add_argument("--new", required=True)
    args = ap.parse_args(argv)
    try:
        running = yaml.safe_load(open(args.running, encoding="utf-8")) or {}
        new = yaml.safe_load(open(args.new, encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"cannot read specs: {exc}", file=sys.stderr)
        return 2
    # `doctl apps spec get` may wrap the spec; accept either shape.
    running = running.get("spec", running)

    d = diff(running, new)
    for label in ("additions", "changes"):
        for line in d[label]:
            print(f"  {label[:-1] if label != 'changes' else 'change'}: {line}")
    if not d["removals"]:
        print(f"  no removals ({len(d['changes'])} change(s), {len(d['additions'])} addition(s))")
        return 0
    print()
    print("  This deploy would REMOVE from the running app:")
    for line in d["removals"]:
        print(f"    - {line}")
    if os.environ.get("ALLOW_SPEC_REMOVALS") == "1":
        print("\n  ALLOW_SPEC_REMOVALS=1 — proceeding, removals acknowledged.")
        return 0
    print("""
  Each of these is either an intended removal or a console edit that
  `doctl apps update` is about to destroy. If it was added in the console,
  put it in infra/digitalocean/app.yaml first. If the removal is intended,
  re-run with ALLOW_SPEC_REMOVALS=1.""")
    return 1


if __name__ == "__main__":
    sys.exit(main())
