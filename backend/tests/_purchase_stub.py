"""Shared stub: the route-flow tests exercise inbound_voice with a fake route
connection; the purchase ledger has its own tests (test_provider_purchases.py)."""


def stub_purchase_ledger(monkeypatch):
    import provider_purchases

    async def buy(ctx, provider, purchase_fn):
        result = await purchase_fn()
        return str(result.detail.get("phone_number") or ""), result.reference, None, True

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(provider_purchases, "buy", buy)
    monkeypatch.setattr(provider_purchases, "attached", noop)
    monkeypatch.setattr(provider_purchases, "record_superseded", noop)
