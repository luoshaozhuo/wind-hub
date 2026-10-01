"""``wind-hub probe`` 的现场辅助探测子包。

每个探测能力一个模块，彼此独立、按需扩展：

- :mod:`~wind_hub.adapter.inbound.cli.probe.models` — 发现结果的数据模型
  （纯逻辑，可单测）。
- :mod:`~wind_hub.adapter.inbound.cli.probe.discover` — 协议点表发现
  （step17：ADS 符号浏览 + Modbus 可选寄存器扫描）。
- :mod:`~wind_hub.adapter.inbound.cli.probe.scan_models` — 网段扫描的
  数据模型与网段解析（纯逻辑）。
- :mod:`~wind_hub.adapter.inbound.cli.probe.scan_methods` — ARP / ICMP /
  TCP 三种扫描方法（step18，仅 Linux）。
- :mod:`~wind_hub.adapter.inbound.cli.probe.scan` — 三层组合编排与
  主机名反查。
- :mod:`~wind_hub.adapter.inbound.cli.probe.ports_models` — 端口扫描的
  三态结果模型与服务映射（纯逻辑）。
- :mod:`~wind_hub.adapter.inbound.cli.probe.ports_parse` — 端口规格
  解析与默认端口集（纯逻辑）。
- :mod:`~wind_hub.adapter.inbound.cli.probe.ports` — TCP connect 端口
  扫描（step19，仅 Linux）。
- :mod:`~wind_hub.adapter.inbound.cli.probe.diagnose_models` — 联通性
  诊断的数据模型（纯逻辑）。
- :mod:`~wind_hub.adapter.inbound.cli.probe.local_info` — 本机网络信息
  获取与同网段判断（step20，仅 Linux）。
- :mod:`~wind_hub.adapter.inbound.cli.probe.diagnose` — 网络层/传输层/
  协议层分层诊断编排（step20，仅 Linux）。
- :mod:`~wind_hub.adapter.inbound.cli.probe.verify_models` — 点表验证
  的结果模型（纯逻辑）。
- :mod:`~wind_hub.adapter.inbound.cli.probe.verify` — 点表只读验证
  （step21：批量读，驱动经只读包装，绝不 write/subscribe）。
"""
