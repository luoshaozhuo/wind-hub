"""从第三方种子生成待填写的 BusinessPoint 映射模板。"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def main() -> None:
    """不猜测业务点名称；业务点列由实际 Gateway 配置审核后填写。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    document = json.loads(args.seed.read_text(encoding="utf-8"))
    points = document["edgeaddrset"]
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["dcems_var_path", "business_point_id", "modbus_addr"],
        )
        writer.writeheader()
        for point in points:
            writer.writerow(
                {
                    "dcems_var_path": point["varPath"],
                    "business_point_id": "",
                    "modbus_addr": point["addr"],
                }
            )


if __name__ == "__main__":
    main()
