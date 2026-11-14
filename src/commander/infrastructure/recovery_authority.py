"""Authenticated external control-fencing authority client.

The remote authority MUST enforce physical write fencing before it responds.
HTTPS authentication is a transport trust boundary, not PLC isolation by itself.
"""

from __future__ import annotations

import asyncio
import base64
import http.client
import json
import ssl
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from commander.application.recovery import RecoveryProof


class IsolationAuthorityClient:
    """Ask a separately deployed authority to atomically fence older writers."""

    def __init__(
        self,
        *,
        url: str,
        ca_cert: Path,
        client_cert: Path,
        client_key: Path,
        instance_id: str,
        timeout: float = 5.0,
    ) -> None:
        target = urlparse(url)
        if (
            target.scheme != "https"
            or not target.hostname
            or target.username
            or target.password
            or target.fragment
        ):
            raise ValueError("recovery authority requires a trusted HTTPS endpoint")
        if not instance_id or timeout <= 0:
            raise ValueError("invalid recovery authority identity or timeout")
        self._host = target.hostname
        self._port = target.port or 443
        self._path = target.path or "/"
        if target.query:
            self._path += "?" + target.query
        self._instance_id = instance_id
        self._timeout = timeout
        self._context = ssl.create_default_context(cafile=str(ca_cert))
        self._context.load_cert_chain(str(client_cert), str(client_key))
        self._context.check_hostname = True
        self._context.verify_mode = ssl.CERT_REQUIRED

    async def verify_and_fence(
        self,
        *,
        device_id: str,
        revision: str,
        caller_id: str,
        request_id: str,
        attestation: bytes,
    ) -> RecoveryProof:
        """Require positive, currently enforced isolation from external authority."""
        payload = json.dumps(
            {
                "device_id": device_id,
                "config_revision": revision,
                "caller_id": caller_id,
                "request_id": request_id,
                "new_instance": self._instance_id,
                "attestation": base64.b64encode(attestation).decode("ascii"),
            },
            separators=(",", ":"),
        ).encode()

        def invoke() -> dict[str, object]:
            connection = http.client.HTTPSConnection(
                self._host, self._port, context=self._context, timeout=self._timeout
            )
            try:
                connection.request(
                    "POST",
                    self._path,
                    body=payload,
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                # Never follow redirects: that could disclose credentials
                # or attestation to a different endpoint.
                if response.status != 200:
                    raise RuntimeError("recovery isolation authority rejected the request")
                data = response.read(8193)
            finally:
                connection.close()
            if len(data) > 8192:
                raise RuntimeError("recovery authority response too large")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError("invalid recovery authority response")
            return result

        result = await asyncio.wait_for(asyncio.to_thread(invoke), timeout=self._timeout + 1)
        # Do not accept merely signed or self-reported status. The trusted
        # authority must assert its *enforced* fencing and I/O isolation.
        if (
            result.get("isolated") is not True
            or result.get("fencing_enforced") is not True
            or result.get("revoked") is not False
        ):
            raise RuntimeError("physical isolation, fencing or revocation status not confirmed")
        epoch = result.get("fencing_epoch")
        if type(epoch) is not int or epoch <= 0:
            raise ValueError("invalid recovery fencing epoch")
        for key, expected in (
            ("device_id", device_id),
            ("config_revision", revision),
            ("new_instance", self._instance_id),
            ("caller_id", caller_id),
            ("request_id", request_id),
        ):
            if result.get(key) != expected:
                raise ValueError("recovery attestation identity mismatch")
        old_instance = result.get("old_instance")
        token_id = result.get("token_id")
        if (
            not isinstance(old_instance, str)
            or not old_instance
            or old_instance == self._instance_id
            or not isinstance(token_id, str)
            or not token_id
        ):
            raise ValueError("invalid recovery token or previous instance")
        expires = result.get("expires_at")
        if not isinstance(expires, str):
            raise ValueError("missing recovery deadline")
        expires_at = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        if expires_at.tzinfo is None or expires_at <= datetime.now(UTC):
            raise ValueError("recovery authority attestation expired")
        return RecoveryProof(
            token_id=token_id,
            device_id=device_id,
            revision=revision,
            old_instance=old_instance,
            new_instance=self._instance_id,
            fencing_epoch=epoch,
            expires_at=expires_at,
        )
