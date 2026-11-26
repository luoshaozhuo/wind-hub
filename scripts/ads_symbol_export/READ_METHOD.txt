ADS 符号表读取方法选择

编辑 scripts/ads_symbol_export/export.py 顶部用户配置：

READ_METHOD = "auto"

可选值：
  auto   默认。依次尝试 ADS 整表、ADS Chunk 分段、ADS 文件服务、
         Windows OCX 枚举，成功即停止；启用状态沿用原有设置。
  full   只尝试 ADS 运行时符号表整表读取；存在部分 PLC 内存风险。
  chunk  只尝试 ADS 运行时符号表分段读取，依次使用 CHUNK_SIZES。
  file   只通过 ADS 文件服务读取 PLC 上已有的 TPY/TMC，
         数据来自文件，不保证与当前运行时变量表一致。
  ocx    只通过 Windows ADS OCX 枚举（实验性；Linux 不支持）。

READ_METHOD 非 auto 时，不会自动切换到其他方法；读取失败时报告错误。
file 与 ocx 模式不会因为 FILE_SERVICE_ENABLED / OCX_ENABLED 为 False 而
被隐式禁用；显式选择的 READ_METHOD 优先于自动模式的启用开关。
其余 ADS IP、AMS Net ID、端口及输出文件设置不变。

运行示例：
  cd scripts/ads_symbol_export
  python export.py

独立测试（需安装 pytest 及 pyads 依赖）：
  python -m pytest tests -q

注意：PLC 真机可用性、OCX 组件及文件服务权限须在现场验证。
