"""IEC104 Sink TCP component test。"""

from __future__ import annotations

import socket

import pytest

from tests.support.iec104_master import IEC104MasterClient
from wind_hub_collector.adapter.outbound.sink.iec104 import IEC104Sink
from wind_hub_core.config import ResolvedSinkConfig
from wind_hub_core.model.point import PointValue
from wind_hub_core.protocol.iec104.codec import (
    CauseOfTransmission,
    TypeID,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _config(port: int) -> ResolvedSinkConfig:
    points = []
    specs = [
        ("rotor.speed", 101, "M_ME_NC_1"),
        ("gen.power", 102, "M_ME_NC_1"),
        ("wind.speed", 103, "M_ME_NC_1"),
        ("status.running", 201, "M_SP_NA_1"),
    ]
    for point_id, ioa, type_id in specs:
        points.append(
            {
                "source": {"device_id": "wtg-001", "point_id": point_id},
                "ref": f"wtg-001.{point_id}",
                "source_data_type": "float32",
                "source_unit": "none",
                "datatype": "float32",
                "unit": "none",
                "address": {"ioa": ioa, "type_id": type_id},
            }
        )
    return ResolvedSinkConfig(
        name="scada",
        type="iec104",
        connection={
            "host": "127.0.0.1",
            "port": port,
            "common_address": 1,
            "batch_size": 2,
        },
        points=points,
    )


@pytest.fixture
async def sink():
    port = _free_port()
    iec104 = IEC104Sink(_config(port))
    await iec104.write(
        [
            PointValue(device_id="wtg-001", point_id="rotor.speed", value=1500.5),
            PointValue(device_id="wtg-001", point_id="gen.power", value=800.0),
            PointValue(device_id="wtg-001", point_id="wind.speed", value=12.5),
            PointValue(device_id="wtg-001", point_id="status.running", value=True),
        ]
    )
    await iec104.open()
    try:
        yield iec104
    finally:
        await iec104.close()


async def test_full_sink_interrogation_and_reject_command(sink) -> None:  # type: ignore[no-untyped-def]
    client = IEC104MasterClient(sink.port)
    await client.connect()
    try:
        await client.startdt()
        asdus = await client.interrogate()
        assert asdus[0].cause == CauseOfTransmission.ACTIVATION_CON
        assert asdus[-1].cause == CauseOfTransmission.ACTIVATION_TERMINATION
        meas = [a for a in asdus if a.type_id == TypeID.M_ME_NC_1]
        sp = [a for a in asdus if a.type_id == TypeID.M_SP_NA_1]
        assert [len(a.objects) for a in meas] == [2, 1]
        values = {o.ioa: o.value for a in meas for o in a.objects}
        assert values == {101: 1500.5, 102: 800.0, 103: 12.5}
        assert sp[0].objects[0].ioa == 201
        reply, negative = await client.send_single_command(201, True)
        assert reply.type_id == TypeID.C_SC_NA_1
        assert reply.cause == CauseOfTransmission.ACTIVATION_CON
        assert negative is True
    finally:
        await client.close()
