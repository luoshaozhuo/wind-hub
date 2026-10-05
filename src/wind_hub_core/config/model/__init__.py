"""配置领域模型包。

按领域拆分的纯 schema 模型：system / unit / device / point / task / sink 与
顶层聚合 :class:`~wind_hub_core.config.model.config.Config`。本层只做 schema
与局部校验，不读取 YAML、不依赖 loader / resolver / validation。
"""
