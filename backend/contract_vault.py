"""Private S3 vault for generated legal contracts.

Contracts are sensitive documents, so they do not use the public media pipeline.
This helper stores PDFs in a private S3 bucket with SSE-S3 (AES256) encryption
at rest and returns short-lived presigned URLs for controlled downloads.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger("oracle.contract_vault")

DEFAULT_BUCKET = "neoh-secure-contracts"
DEFAULT_EXPIRATION_SECONDS = 3600
MAX_EXPIRATION_SECONDS = 3600
MAX_PDF_BYTES = int(os.getenv("CONTRACT_VAULT_MAX_BYTES", str(10 * 1024 * 1024)))
_SAFE_DOCUMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class VaultUploadError(RuntimeError):
    """Raised when a contract cannot be stored or signed safely."""


@dataclass(frozen=True)
class VaultedContract:
    document_id: str
    bucket: str
    s3_key: str
    presigned_url: str
    expires_in: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SovereignVault:
    """S3-backed encrypted contract vault.

    boto3 uses the standard credential chain, so local `.env` credentials, an
    AWS_PROFILE, or an ECS task role all work without hardcoding secrets.
    """

    def __init__(
        self,
        *,
        s3_client: Any = None,
        bucket_name: Optional[str] = None,
        region_name: Optional[str] = None,
    ):
        self.bucket_name = (bucket_name or os.getenv("CONTRACT_VAULT_BUCKET") or DEFAULT_BUCKET).strip()
        if not self.bucket_name:
            raise ValueError("CONTRACT_VAULT_BUCKET cannot be empty")

        self.region_name = region_name or os.getenv("AWS_REGION", "us-east-2")
        if s3_client is not None:
            self.s3_client = s3_client
        else:
            import boto3  # lazy import; only vault users need the SDK

            self.s3_client = boto3.Session(region_name=self.region_name).client("s3")

    @staticmethod
    def _client_id(value: str) -> str:
        try:
            return str(uuid.UUID(str(value)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("client_id must be a UUID") from exc

    @staticmethod
    def _document_id(value: str) -> str:
        document_id = str(value).strip()
        if not _SAFE_DOCUMENT_ID.match(document_id):
            raise ValueError("document_id contains unsupported characters")
        return document_id

    @staticmethod
    def _expiration(value: int) -> int:
        try:
            seconds = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("expiration_seconds must be an integer") from exc
        if seconds < 1 or seconds > MAX_EXPIRATION_SECONDS:
            raise ValueError(
                f"expiration_seconds must be between 1 and {MAX_EXPIRATION_SECONDS}"
            )
        return seconds

    @staticmethod
    def _validate_pdf(pdf_file_path: str | os.PathLike[str]) -> Path:
        path = Path(pdf_file_path)
        if not path.is_file():
            raise ValueError("pdf_file_path does not point to a file")
        size = path.stat().st_size
        if size <= 0:
            raise ValueError("PDF file is empty")
        if size > MAX_PDF_BYTES:
            raise ValueError(f"PDF exceeds {MAX_PDF_BYTES} byte vault limit")
        with path.open("rb") as fh:
            if fh.read(5) != b"%PDF-":
                raise ValueError("File is not a PDF")
        return path

    def s3_key(self, client_id: str, document_id: str) -> str:
        safe_client_id = self._client_id(client_id)
        safe_document_id = self._document_id(document_id)
        return f"clients/{safe_client_id}/contracts/{safe_document_id}.pdf"

    def encrypt_and_upload(self, pdf_file_path: str, client_id: str, document_id: str) -> bool:
        """Upload a generated PDF and force SSE-S3 AES256 encryption at rest."""
        try:
            path = self._validate_pdf(pdf_file_path)
            key = self.s3_key(client_id, document_id)
            logger.info("Contract vault upload starting: document_id=%s", document_id)
            self.s3_client.upload_file(
                str(path),
                self.bucket_name,
                key,
                ExtraArgs={
                    "ServerSideEncryption": "AES256",
                    "ContentType": "application/pdf",
                    "ContentDisposition": f'attachment; filename="{document_id}.pdf"',
                },
            )
            logger.info("Contract vault upload complete: document_id=%s", document_id)
            return True
        except (BotoCoreError, ClientError, OSError, ValueError) as exc:
            logger.error("Contract vault upload failed for document_id=%s: %s", document_id, exc)
            return False

    def generate_expiring_link(
        self,
        client_id: str,
        document_id: str,
        expiration_seconds: int = DEFAULT_EXPIRATION_SECONDS,
    ) -> Optional[str]:
        """Generate a presigned GET URL that expires after at most one hour."""
        try:
            expires_in = self._expiration(expiration_seconds)
            key = self.s3_key(client_id, document_id)
            return self.s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket_name, "Key": key},
                ExpiresIn=expires_in,
            )
        except (BotoCoreError, ClientError, ValueError) as exc:
            logger.error("Contract vault presign failed for document_id=%s: %s", document_id, exc)
            return None

    def vault_pdf(
        self,
        pdf_file_path: str | os.PathLike[str],
        *,
        client_id: str,
        document_id: str,
        expiration_seconds: int = DEFAULT_EXPIRATION_SECONDS,
    ) -> VaultedContract:
        """Upload a PDF and return the signed download descriptor."""
        key = self.s3_key(client_id, document_id)
        if not self.encrypt_and_upload(str(pdf_file_path), client_id, document_id):
            raise VaultUploadError("contract upload failed")

        url = self.generate_expiring_link(client_id, document_id, expiration_seconds)
        if not url:
            raise VaultUploadError("contract upload succeeded but presigned URL failed")

        return VaultedContract(
            document_id=self._document_id(document_id),
            bucket=self.bucket_name,
            s3_key=key,
            presigned_url=url,
            expires_in=self._expiration(expiration_seconds),
        )
