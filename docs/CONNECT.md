# Подключение MCP-Hands к MCP-клиенту

MCP-Hands — обычный сервер Model Context Protocol: stdio-транспорт, один процесс на клиента,
38 инструментов. Ниже — как подключить его к клиенту и как убедиться, что всё живо.

## 1. Подготовка окружения

```powershell
cd D:\BOT\MCP-Hands
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pytest -q      # 248 passed на живом рабочем столе
```

LLM (нужна только для сложных целей и планирования): скопируйте `config.example.json` в
`config.local.json` рядом с проектом и заполните `llm.base_url`, `llm.api_key`, `llm.model`.
Ключ хранится только в этом файле (он в `.gitignore`) и никогда не пишется в журналы.

## 2. Конфиг клиента

ZCode (`~/.zcode/…`) или Claude Desktop (`%APPDATA%\Claude\claude_desktop_config.json`) —
секция `mcpServers`:

```json
{
  "mcpServers": {
    "mcp-hands": {
      "command": "D:/BOT/MCP-Hands/.venv/Scripts/python.exe",
      "args": ["-m", "mcp_hands.mcp_server"]
    }
  }
}
```

Путь к интерпретатору подставьте свой. Сервер поднимается лениво: тяжёлые зависимости
(comtypes, winsdk, dxcam) тянутся при первом вызове инструмента.

## 3. Проверка без клиента

Пульт открывает настоящую MCP-сессию со своим сервером — удобно для ручной приёмки:

```powershell
.venv\Scripts\python.exe tools\hands_cli.py
mcp-hands> health
mcp-hands> tools
mcp-hands> call windows_list {}
mcp-hands> demo
```

stderr сервера пишется в `logs/mcp_server_stderr.log`.

## 4. Что помнить при работе через MCP

- **Стоп-лист работает отсутствием**: инструментов отправки, оплаты, публикации и удаления в
  каноне нет; удаление файлов возможно только в корзину (`fs_trash_move`).
- **Клавиатура — только в проверенно активное окно**: у `type_text`/`hotkey` обязателен
  `expect_hwnd`, иначе инструмент возвращает отказ (это нормальный вердикт, а не сбой).
- **Живой браузер владельца** подключается только к экземпляру, запущенному с отладочным
  портом (`tools/opera_debug_launcher.py`). Вкладки владельца никогда не закрываются и не
  переставляются: агент открывает свою вкладку.
- **Сухой прогон и оверлей подсветки** — там, где действие необратимо; журнал каждой операции
  лежит в `logs/mcp_calls.log` (тексты ввода не записываются).

## 5. Голосовой ассистент

Ассистент использует те же инструменты, но вызывается голосом: `tools/jarvis_panel.py` —
настройка микрофона и голоса, `tools/jarvis.py` — сам ассистент (по умолчанию режим разговора:
одна Ctrl+Alt+J включает микрофон, пауза усыпляет).
