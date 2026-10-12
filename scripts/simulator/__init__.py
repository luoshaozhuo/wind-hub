"""wind-hub 多协议 PLC 模拟器。

默认拓扑：20 Modbus + 10 ADS + 10 IEC104 = 40 台风机设备，全部真实
协议栈（pymodbus / pyads testserver / c104），与 ``configs/simulation/``
中的真实 Core 配置一一对应——Collector / Commander / Web 全部组件可以
直连本模拟器做端到端验收。

端口 / 地址规划（全部本机回环，互不冲突）：

- Modbus ``sim-mb-01..20``：127.0.0.1:10502+i（i = 0..19）
- IEC104 ``sim-i104-01..10``：127.0.0.1:12404+i（i = 0..9）
- ADS ``sim-ads-01..10``：127.0.0.11+i:48898（pyads 客户端 TCP 目的端口
  固定 48898；以不同 loopback IP 区分设备，AMS Net ID = ``<ip>.1.1``）

故障注入经 ``run/faults.json``（fault/recover 子命令写入，运行进程周期
轮询并应用）：``down`` 停掉该设备 server；``freeze`` 冻结数据生成。
"""
