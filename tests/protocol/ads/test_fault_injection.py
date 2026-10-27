"""ADS 地址不合法时不建立网络事务。"""

from __future__ import annotations

import pytest

from core.application.errors import ConfigError
from core.domain import ConnectionEndpoint
from core.infrastructure.protocol.ads import ADSDriver
from tests.protocol.conftest import make_point, make_table


def test_ads_invalid_local_point_is_rejected_before_connect() -> None:
    with pytest.raises(ConfigError, match="size"):
        ADSDriver(
            ConnectionEndpoint("127.0.0.1", 851),
            make_table(
                "ads",
                make_point("bad", symbol="MAIN.bad", data_type="REAL", size=3),
            ),
            {"target_net_id": "1.2.3.4.5.6"},
        )
