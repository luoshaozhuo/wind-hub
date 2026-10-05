"""Raw 配置模型到 resolved 运行时模型的解析包。

device（实例 + 型号合并）、point_table（继承展开）、sink（source 引用解析）
三个 resolver 只做纯配置转换，依赖 ``config.model``，不创建运行时资源。
"""
