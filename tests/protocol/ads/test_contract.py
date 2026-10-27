"""ADS ProtocolPort 已实现能力的公开契约。"""

from __future__ import annotations

from core.application.protocol_contract import ProtocolCapability
from core.domain import ConnectionEndpoint, PointAccess
from core.infrastructure.protocol.ads import ADSDriver
from tests.protocol.conftest import make_point, make_table


def test_ads_capabilities() -> None:
    point = make_point(
        "speed", access=PointAccess.READ, symbol="MAIN.speed", data_type="REAL"
    )
    driver = ADSDriver(
        ConnectionEndpoint("127.0.0.1", 851),
        make_table("ads", point),
        {"target_net_id": "1.2.3.4.5.6"},
    )
    assert driver.capabilities() == frozenset({
        ProtocolCapability.READ, ProtocolCapability.READ_MANY, ProtocolCapability.WRITE,
        ProtocolCapability.WRITE_MANY, ProtocolCapability.SUBSCRIBE,
    })
    assert not driver.is_open()
