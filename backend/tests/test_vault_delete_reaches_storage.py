"""Erasure can delete contract PDFs from the vault (found reviewing 29.8b).

privacy_lifecycle imported `ContractVault`, which does not exist (the class is
SovereignVault), and the default storage adapter had no delete_object: every
vault deletion during a brokerage erasure failed, so since the 22.x fix such an
erasure could never succeed.
"""
from __future__ import annotations

import inspect

import object_storage


def test_the_erasure_code_imports_a_vault_that_exists():
    import contract_vault
    import privacy_lifecycle
    import privacy_requests

    for module in (privacy_lifecycle, privacy_requests):
        assert "import SovereignVault" in inspect.getsource(module)
    assert hasattr(contract_vault, "SovereignVault")


def test_the_default_vault_adapter_deletes_through_object_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(object_storage, "BACKEND", "local")
    monkeypatch.setattr(object_storage, "MEDIA_ROOT", tmp_path)
    object_storage.put_bytes("tenants/t1/contracts/doc.pdf", b"%PDF-1.4", "application/pdf")
    from contract_vault import SovereignVault

    vault = SovereignVault(s3_client=object_storage.BotoCompatibleStore(), bucket_name="vault")
    vault.s3_client.delete_object(Bucket=vault.bucket_name, Key="tenants/t1/contracts/doc.pdf")
    assert not (tmp_path / "tenants/t1/contracts/doc.pdf").exists()
