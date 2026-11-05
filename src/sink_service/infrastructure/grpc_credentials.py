"""mTLS server credentials for the one Main control endpoint."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import grpc


def server_credentials(security: dict[str, Any]) -> grpc.ServerCredentials:
    key = Path(security["server_key"]).read_bytes()
    certificate = Path(security["server_cert"]).read_bytes()
    client_ca = Path(security["ca_cert"]).read_bytes()
    return grpc.ssl_server_credentials(
        [(key, certificate)],
        root_certificates=client_ca,
        require_client_auth=True,
    )
