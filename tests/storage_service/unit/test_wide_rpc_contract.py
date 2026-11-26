"""QuerySamples generated wire contract and typed value fidelity."""

from google.protobuf.timestamp_pb2 import Timestamp

from contracts import storage_service_pb2 as pb


def test_grouped_history_rpc_registered_with_exact_message_contract() -> None:
    service = pb.DESCRIPTOR.services_by_name["StorageService"]
    method = service.methods_by_name["QuerySamples"]
    assert method.input_type.full_name == "windhub.storage.v2.QuerySamplesRequest"
    assert method.output_type.full_name == "windhub.storage.v2.QuerySamplesResponse"
    assert "QueryWideRows" not in service.methods_by_name


def test_typed_grouped_row_roundtrip_keeps_unsigned_null_and_quality() -> None:
    stamp = Timestamp(seconds=1795608000, nanos=1000)
    original = pb.QuerySamplesResponse(
        groups=[
            pb.StorageGroup(
                wind_farm_id="farm",
                device_id="wtg1",
                device_type="turbine",
                point_table_id="point_table_wtg_v1.0.0",
                data_level=pb.STORAGE_DATA_LEVEL_PROTOCOL,
                schema_fingerprint="a" * 64,
                columns=[
                    pb.StorageColumn(
                        point_id="unsigned",
                        business_point_id="business_unsigned",
                        unit="count",
                        data_type="uint64",
                        physical_name="p_unsigned_v1_0_0",
                    ),
                    pb.StorageColumn(
                        point_id="missing",
                        business_point_id="business_missing",
                        unit="m/s",
                        data_type="double",
                        physical_name="p_missing_v1_0_0",
                    ),
                ],
                rows=[
                    pb.StorageWideRow(
                        sample_time=stamp,
                        values=[
                            pb.StorageValue(value_u64=2**64 - 1),
                            pb.StorageValue(),
                        ],
                        quality=b"\x01",
                    )
                ],
            )
        ],
        complete=True,
    )
    decoded = pb.QuerySamplesResponse.FromString(original.SerializeToString())
    group = decoded.groups[0]
    assert group.columns[0].unit == "count"
    assert group.columns[0].physical_name == "p_unsigned_v1_0_0"
    assert group.rows[0].values[0].value_u64 == 2**64 - 1
    assert group.rows[0].values[0].WhichOneof("value") == "value_u64"
    assert group.rows[0].values[1].WhichOneof("value") is None
    assert group.rows[0].quality == b"\x01"
    assert decoded.complete
