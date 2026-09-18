"""vendor 容器包。

vendor/nonebot_plugin_parser_lite 是上游 standalone 分支的零修改快照
（ADR-0001）。此 __init__.py 属于桥接侧，用于把 vendor 目录声明为常规子包，
使内部相对导入在 `data.plugins.<插件名>.vendor` 命名空间下有效。
"""
