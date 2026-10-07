"""Collector Application——采集用例编排与运行时子系统。

承载采集引擎装配（Device/Task/Sink 三个子 Runtime）、设备会话、
配置热重载编排（prepare/activate/abort）。只依赖 ``core`` 与
``collector.domain``；不得 import 本进程的 Infrastructure 具体类。
"""
