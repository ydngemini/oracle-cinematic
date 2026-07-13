"""Contract vault tests.

The fake S3 client lets us verify encryption and presign behavior without
network access or AWS credentials.
"""

from pathlib import Path

from contract_vault import SovereignVault


CLIENT_ID = "11111111-1111-1111-1111-111111111111"
DOCUMENT_ID = "22222222-2222-4222-8222-222222222222"


class FakeS3:
    def __init__(self):
        self.uploads = []
        self.presigns = []

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.uploads.append(
            {
                "filename": filename,
                "bucket": bucket,
                "key": key,
                "ExtraArgs": ExtraArgs or {},
            }
        )

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.presigns.append(
            {"operation": operation, "Params": Params, "ExpiresIn": ExpiresIn}
        )
        return f"https://example.test/{Params['Key']}?signed=1"


def _pdf(tmp_path: Path) -> Path:
    path = tmp_path / "assignment.pdf"
    path.write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n")
    return path


def test_encrypt_and_upload_forces_aes256_pdf_args(tmp_path):
    fake = FakeS3()
    vault = SovereignVault(s3_client=fake, bucket_name="contract-bucket")

    ok = vault.encrypt_and_upload(str(_pdf(tmp_path)), CLIENT_ID, DOCUMENT_ID)

    assert ok is True
    assert fake.uploads[0]["bucket"] == "contract-bucket"
    assert fake.uploads[0]["key"] == f"clients/{CLIENT_ID}/contracts/{DOCUMENT_ID}.pdf"
    assert fake.uploads[0]["ExtraArgs"]["ServerSideEncryption"] == "AES256"
    assert fake.uploads[0]["ExtraArgs"]["ContentType"] == "application/pdf"
    assert "ACL" not in fake.uploads[0]["ExtraArgs"]


def test_generate_expiring_link_uses_one_hour_expiry(tmp_path):
    fake = FakeS3()
    vault = SovereignVault(s3_client=fake, bucket_name="contract-bucket")

    url = vault.generate_expiring_link(CLIENT_ID, DOCUMENT_ID)

    assert url.startswith("https://example.test/")
    assert fake.presigns[0]["operation"] == "get_object"
    assert fake.presigns[0]["ExpiresIn"] == 3600


def test_generate_expiring_link_rejects_over_one_hour():
    fake = FakeS3()
    vault = SovereignVault(s3_client=fake, bucket_name="contract-bucket")

    assert vault.generate_expiring_link(CLIENT_ID, DOCUMENT_ID, 3601) is None
    assert fake.presigns == []


def test_rejects_non_pdf_upload(tmp_path):
    fake = FakeS3()
    vault = SovereignVault(s3_client=fake, bucket_name="contract-bucket")
    bad = tmp_path / "not.pdf"
    bad.write_text("not a pdf")

    assert vault.encrypt_and_upload(str(bad), CLIENT_ID, DOCUMENT_ID) is False
    assert fake.uploads == []
