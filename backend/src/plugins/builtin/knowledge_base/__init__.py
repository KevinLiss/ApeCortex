"""AI 知识库插件（P0：记忆层）。

.. note::
   插件管理器发现插件时，会 import 本包的 ``__init__``，
   然后在其中查找 ``PluginInterface`` 的子类。
"""
from src.plugins.builtin.knowledge_base.plugin import KnowledgeBasePlugin

__all__ = ["KnowledgeBasePlugin"]
