ADS 符号表读取方法选择（Linux）

编辑 ads_symbol_export_v1.0.0.yaml，所有运行参数集中在该文件中。
READ_METHOD: auto
  auto  按配置尝试整表、分段、文件服务，成功即停止。
  full  仅整表读取；部分 PLC 存在内存溢出风险。
  chunk 仅分段读取；依次使用 CHUNK_SIZES。
  file  仅通过 ADS 文件服务读取 TPY/TMC；不保证与运行时一致。
指定方法失败不会跨方法回退。TRY_FULL_TABLE_FIRST 和 FILE_SERVICE_ENABLED
仅在 auto 模式下控制是否尝试该方法。

运行：
  cd scripts/ads_symbol_export
  python -m pip install -e .
  python export.py --config ads_symbol_export_v1.0.0.yaml

测试见 TESTING.txt。无需 wind-hub 主程序。
