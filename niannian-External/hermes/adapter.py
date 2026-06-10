#!/usr/bin/env python3
"""
hermes/adapter.py — 念念↔Hermes 适配器
========================================
当念念需要借Hermes的脑时，通过此文件标准化调用。

当前阶段（v0.1）：
  直接通过 bridge.call_hermes() 调用 Hermes CLI。
  未来可扩展：通过MCP协议直接对接Hermes Gateway。

用法：
  from bridge import Bridge
  bridge = Bridge()
  result = bridge.call_hermes("分析一下BTC走势")
"""
# 当前适配逻辑在 bridge.py 的 Bridge.call_hermes()
# 此文件为未来扩展预留——例如：
#   - 通过Hermes Gateway API调用
#   - 将niannian-identities注入到Hermes session
#   - 双向通信协议
