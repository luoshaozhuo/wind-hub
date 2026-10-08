"""Commander Domain 预留层。

Commander 的设备/点/配置领域对象全部复用 ``core.domain``；进程特有领域
概念（Command、CommandResult 等用例模型）当前随 Application 用例定义。
本子包保留三层契约中的 Domain 层位置，后续出现真正的进程内领域规则时
（无 I/O、无协议库依赖）再放这里。
"""
