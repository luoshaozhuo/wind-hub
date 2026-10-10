"""CSV 序列化契约，重点覆盖值类型和转义。"""

import csv
import io
from datetime import UTC, datetime

from core.application.csv_sink_record import encode_csv_batch
from core.domain.point_value import PointValue


def test_csv_header_and_values_are_readable() -> None:
    timestamp = datetime(2026, 10, 10, tzinfo=UTC)
    values = [
        PointValue("WT001", "power", 123.5, timestamp=timestamp),
        PointValue("WT001", "alarm", None, timestamp=timestamp, source="modbus"),
    ]
    output = encode_csv_batch(values, include_header=True).decode("utf-8")
    rows = list(csv.reader(io.StringIO(output)))
    assert rows[0] == ["timestamp", "device_id", "point_id", "value", "quality", "source"]
    assert rows[1] == ["2026-10-10T00:00:00Z", "WT001", "power", "123.5", "good", ""]
    assert rows[2][3:] == ["", "good", "modbus"]


def test_csv_escapes_quotes_commas_and_newlines() -> None:
    item = PointValue("WT,001", "message", 'a,"b"\nc', source="ads")
    rows = list(csv.reader(io.StringIO(encode_csv_batch([item]).decode("utf-8"))))
    assert rows[0][1] == "WT,001"
    assert rows[0][3] == 'a,"b"\nc'


def test_empty_batch_has_no_content_unless_header_requested() -> None:
    assert encode_csv_batch([]) == b""
    assert encode_csv_batch([], include_header=True).startswith(b"timestamp,")
