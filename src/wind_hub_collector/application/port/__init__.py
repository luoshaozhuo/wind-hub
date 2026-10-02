"""Collector Application 层 outbound port。

这里只保留 Collector Runtime 直接依赖、由 outbound adapter 实现的应用端口。
协议驱动端口 ProtocolPort 属于 domain 扩展点，继续位于 domain.port。
"""
