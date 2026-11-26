ADS 符号表读取方法选择（Linux）

编辑 ads_symbol_export_v1.0.0.yaml，所有运行参数集中在该文件中。
READ_METHOD: auto
  auto  按配置尝试整表、分段、文件服务，成功即停止。
  full  仅整表读取；部分 PLC 存在内存溢出风险。
  chunk 仅分段读取；依次使用 CHUNK_SIZES。
  file  直接连接 System Service（AMS port 10000），不访问 Runtime；
        读取 TPY/TMC，结果不保证与运行时一致。
指定方法失败不会跨方法回退。TRY_FULL_TABLE_FIRST 和 FILE_SERVICE_ENABLED
仅在 auto 模式下控制是否尝试该方法。auto 在 Runtime 初次连接失败时，
若文件服务已启用，也可直接尝试文件服务。
文件服务直接执行查找/打开/读取请求，不要求 ReadState 支持。
所有模式均输出 UTF-8 纯变量名列表，每行一个名称，无标题或注释。
文件来源、远端路径及验证等级通过日志记录。
Runtime 合法空符号表（count=0, total=0）成功输出零字节文件，不回退。
TC3 默认 TMC 搜索路径（Boot\Plc、Boot）是启发式；实际固件布局可能不同，
用 TPY_SEARCH_PATHS 指定真实路径。
整表读取受 ADS 服务端及 AdsLib 单次请求能力限制；显式 full 失败会明确报告，
auto 模式下可回退分段。真实 PLC 兼容性仍需现场验证。

运行：
  cd scripts/ads_symbol_export
  python -m pip install -e .
  python export.py --config ads_symbol_export_v1.0.0.yaml

测试见 TESTING.txt。无需 wind-hub 主程序。
