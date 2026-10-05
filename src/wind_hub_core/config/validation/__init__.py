"""跨文件一致性校验包。

对已完成 resolve 的配置模型做引用关系、协议地址与 Task 组合约束校验：
纯函数、无 I/O、不创建运行时对象、不依赖 collector / server / commander。
"""
