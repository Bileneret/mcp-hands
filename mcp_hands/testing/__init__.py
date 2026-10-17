"""Тестовые мишени: окно с кнопками и окно, перестающее отвечать.

Живут в пакете, а не в `spike/`, потому что нужны не только историческим замерам,
но и автотестам ядра, и ручной проверке на живом стенде:

    python -m mcp_hands.testing.fixture_window --title "MCP-Hands target"
    python -m mcp_hands.testing.hang_window --responsive-sec 0.5
"""
