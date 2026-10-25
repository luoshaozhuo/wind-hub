"""core 可靠性层 fixture：复用 integration 层的真实 AdsTestServer fixture。"""

from tests.integration.protocols.ads.conftest import ads_service  # noqa: F401
