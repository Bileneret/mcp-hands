# Аудит ревизии v2 ядра MCP-Hands (коммит `81126c2`)

> **Дата аудита**: 17.10.2026  
> **Проверяемый коммит**: `81126c2`  
> **Входные данные**: `ForAgentsSpeak/to_review/STAGE_INFO_v2.md`, `ОТВЕТ_НА_АУДИТ_09_v1.md`, каталог `v2/`  
> **Стенд**: Windows 11 (build 26120), Python 3.12.0, comtypes, dxcam, mss, winsdk, pytest-9.1.1  
> **Результат прогона тест-сьюта на старте ревью**: 1 FAILED, 40 PASSED, 11 SKIPPED из 52 тестов.

---

## 1. Сводный реестр дефектов и замечаний по ревизии v2

### [Критичность: Blocker]
- **Проблема**: `mcp_hands/screen.py`, строки 173–178 и 180–195. Полный отказ `crop_window` на максимизированных окнах (развёрнутых на весь экран).
  В Windows у максимизированных окон (Блокнот, Word, браузеры) невидимые границы изменения размера DWM выходят за пределы физического экрана: `GetWindowRect` или `DwmGetWindowAttribute` возвращают прямоугольник вида `{"left": -8, "top": -8, "right": width + 8, "bottom": height + 8}`.
  Метод `covers()` выполняет строгую проверку полного включения:
  ```python
  return (rect["left"] >= offset_x and rect["top"] >= offset_y
          and rect["right"] <= offset_x + width and rect["bottom"] <= offset_y + height)
  ```
  Для максимизированного окна условие `-8 >= 0` ложно. Метод `crop_window` решает, что окно не помещается в кадр DXGI, и уходит на резервный путь захвата всего виртуального экрана через BitBlt (`prefer="mss"`). Однако для виртуального экрана с началом `(0, 0)` проверка `self.covers(base, rect)` **снова возвращает False**! В строке 191 выбрасывается исключение:
  `ScreenCaptureError: окно {'left': -8, 'top': -8, 'right': 1928, 'bottom': 1088} не попало даже в кадр всего виртуального экрана...`
  В итоге агент не может сделать скриншот ни одного развёрнутого на весь экран окна!
- **Ломающий сценарий (тест-кейс)**:
  ```python
  def test_crop_window_fails_on_maximized_window():
      from mcp_hands.screen import ScreenGrabber, Frame
      import numpy as np
      grabber = ScreenGrabber()
      # Кадр виртуального экрана 1920x1080
      base = Frame(image=np.zeros((1080, 1920, 4), dtype=np.uint8), source="mss", origin=(0, 0), ms=1.0)
      # Развёрнутое окно с штатными рамками DWM -8 px
      maximized_rect = {"left": -8, "top": -8, "right": 1928, "bottom": 1088}
      class MockSpace:
          def window_frame(self, hwnd):
              return maximized_rect
      # Вызов упадет с ScreenCaptureError
      grabber.crop_window(MockSpace(), hwnd=123, frame=base)
  ```
- **Что исправить кодеру**:
  В `covers()` проверять не строгое геометрическое включение (bounding inclusion), а непустое пересечение (intersection) с видимой частью экрана:
  ```python
  @staticmethod
  def covers(frame: Frame, rect: dict) -> bool:
      offset_x, offset_y = frame.origin
      height, width = frame.image.shape[0], frame.image.shape[1]
      inter_left = max(offset_x, rect["left"])
      inter_top = max(offset_y, rect["top"])
      inter_right = min(offset_x + width, rect["right"])
      inter_bottom = min(offset_y + height, rect["bottom"])
      return inter_right > inter_left and inter_bottom > inter_top
  ```
  Метод `crop()` уже содержит правильное ограничение `max(0, min(width, ...))`, поэтому после исправления `covers()` максимизированное окно будет обрезано по видимой области экрана без падений.

---

### [Критичность: Blocker]
- **Проблема**: `mcp_hands/probe_worker.py`, строки 23–32 и `mcp_hands/input.py`, строки 180–200, 254–281. Изоляция рабочего стола в сателлите приводит к отказу `SendInput` с `ERROR_ACCESS_DENIED (5)`.
  В реальном прогоне `pytest` тест `test_click_reaches_element` в `tests/test_probe.py` падает:
  `AssertionError: {'ok': False, 'target': [549, 423], 'landed': [0, 0], 'events': 0, 'cursor_check': 'недоступен'}`.
  Анализ показал: это не просто «отсутствие курсора в headless-сессии», а изоляция рабочих столов в Windows. Процессы-сателлиты (`multiprocessing.Process`), процессы служб и процессов, запускаемых через IDE/скрипты, порождаются на фоновом десктопе родителя, в то время как интерактивный ввод принимает десктоп `Default`.
  На несогласованном десктопе:
  1. `GetCursorPos` возвращает `(0, 0)`.
  2. `SendInput` немедленно завершается с возвратом `0` и ошибкой `ERROR_ACCESS_DENIED` (`GetLastError() == 5`).
  3. `Actuator._send` молча возвращает `0`.
  4. `Actuator.click` получает `sent == 0`, выставляет `ok: False`, но оставляет поле `reason` пустым.
  При этом если вызвать в потоке актуатора:
  ```python
  hdesk = user32.OpenInputDesktop(0, False, 0x01FF)
  if hdesk:
      user32.SetThreadDesktop(hdesk)
  ```
  поток мгновенно получает доступ к интерактивному рабочему столу: `GetCursorPos` начинает возвращать реальные координаты мыши, а `SendInput` успешно отправляет события (`sent == 2`).
- **Ломающий сценарий (тест-кейс)**:
  Живой тест `test_click_reaches_element` стабильно падает на любой машине, где сателлит выполняется в изолированном десктопе потока.
- **Что исправить кодеру**:
  1. В `Worker.__init__` (`probe_worker.py`) до создания окон и COM-объектов вызывать синхронизацию с рабочим столом ввода:
     ```python
     user32 = ctypes.windll.user32
     hdesk = user32.OpenInputDesktop(0, False, 0x01FF)
     if hdesk:
         user32.SetThreadDesktop(hdesk)
     ```
  2. В `Actuator._send` (`input.py`) проверять возврат `SendInput`: если результат равен 0, запрашивать `ctypes.get_last_error()`. При ошибке 5 предпринимать попытку синхронизации с `OpenInputDesktop` и повторной отправки, либо возвращать статус с понятной причиной: `"SendInput blocked by desktop isolation or UIPI (WinError 5)"`.
  3. В `Actuator.click` при `sent != 2` обязательно заполнять поле `reason` причиной сбоя ввода, а не возвращать `None`.

---

### [Критичность: Major]
- **Проблема**: `mcp_hands/input.py`, строки 266–277. Ослабление защиты от промаха в `click()`: слепой клик в неизвестные координаты при `cursor_available == False`.
  Кодер спросил: *«Ослаблена ли защита от промаха клика?»* — **Ответ: да, ослаблена.**
  В текущей реализации:
  ```python
  cursor_available = not (landed == (0, 0) and (x, y) != (0, 0))
  if verify and cursor_available and (abs(landed[0] - x) > tolerance or ...):
      return {"ok": False, "cursor_check": "промах", ...}
  sent = self._send((INPUT_MOUSE, MOUSEEVENTF_LEFTDOWN, 0, 0, 0),
                    (INPUT_MOUSE, MOUSEEVENTF_LEFTUP, 0, 0, 0))
  ```
  Если `landed == (0, 0)`, проверка `verify` полностью игнорируется! Метод считает, что «курсор недоступен», и вызывает `_send` с относительными координатами `(0, 0)`.
  В Win32 `SendInput` при `dx=0, dy=0` без `MOUSEEVENTF_ABSOLUTE` выполняет клик **в текущей позиции курсора ОС**. Если перемещение `self.move(x, y)` не сработало (например, из-за сбоя драйвера, рассинхронизации экранов или блокировки ввода), курсор остался на предыдущей позиции (например, на кнопке закрытия чужого окна или меню «Пуск») — и актуатор слепо нажимает туда!
- **Ломающий сценарий (тест-кейс)**:
  Элемент находится в точке `(600, 400)`. Мышь физически стоит на кнопке «Закрыть» окна `(1910, 10)`. Из-за ошибки десктопа `GetCursorPos` возвращает `(0, 0)`. Актуатор игнорирует проверку и кликает в `(1910, 10)`, закрывая окно вместо нажатия кнопки.
- **Что исправить кодеру**:
  1. Если `verify=True` и позиция курсора не может быть подтверждена, нажатие мыши должно блокироваться:
     `{"ok": False, "cursor_check": "недоступен", "reason": "позиция курсора не подтверждена — слепой клик заблокирован"}`.
  2. Допустить пропуск верификации только при явном флаге `verify=False` или специальном режиме `allow_unverified=True`.
  3. События `MOUSEEVENTF_LEFTDOWN` и `UP` отправлять **с абсолютными координатами цели**, а не с `(0, 0)`:
     ```python
     nx, ny = self.space.to_normalized(x, y)
     self._send((INPUT_MOUSE, MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE, nx, ny, 0),
                (INPUT_MOUSE, MOUSEEVENTF_LEFTUP | MOUSEEVENTF_ABSOLUTE, nx, ny, 0))
     ```
     Тогда даже при рассинхронизации Windows аппаратно переместит точку нажатия в целевой элемент.

---

### [Критичность: Major]
- **Проблема**: `mcp_hands/probe.py`, строки 102–133. Архитектурный разрыв опеки процессов Office между сателлитом и супервизором.
  Кодер реализовал `track_process`, `untrack_process` и `kill_tracked` в классе `ProbeProcess`.
  Однако `ProbeProcess` исполняется в **родительском процессе**, а вызовы COM Office будут происходить внутри воркера `probe_worker.py` (дочерний процесс).
  В текущем API у воркера нет канала, чтобы асинхронно уведомить родителя: *«Я только что запустил `WINWORD.EXE` с PID 5432, возьми его под опеку»*.
  Если в будущем `office.py` вызов `word.Documents.Open(...)` зависнет внутри COM RPC (на поврежденном файле или модальном окне) — метод воркера **не вернет управление родителю**! Родитель по таймауту выполнит `kill()`, но список `self._tracked` в родителе останется пустым! Процесс `WINWORD.EXE` снова утечет и останется висеть в памяти.
  Кроме того, вызов `taskkill /F /T /PID <pid>` без проверки имени процесса опасен из-за переиспользования PID (PID recycling) в Windows.
- **Ломающий сценарий (тест-кейс)**:
  Воркер создает Word, начинает открытие документа и зависает внутри вызова. Родитель срубает воркер по таймауту. Воркер не успел ответить на запрос, родитель не знал PID, процесс Word остался зомби.
- **Что исправить кодеру**:
  1. Запуск экземпляра Word должен быть выделен в отдельную операцию `op_start_word()`, которая возвращает `word_pid` родителю. Родитель немедленно регистрирует `word_pid` через `track_process(word_pid)` ДО отправки запроса на открытие документа.
  2. Либо воркер при старте любого внешнего процесса записывает его PID в разделяемый файл реестра активных процессов сессии (например, `.active_pids.json`), который супервизор считывает при любом аварийном `kill()`.
  3. В команду `taskkill` добавить фильтр по имени образа:
     `taskkill /F /T /FI "IMAGENAME eq WINWORD.EXE" /PID <pid>`.

---

### [Критичность: Minor]
- **Проблема**: `v2/tests/test_regressions_audit.py`. Мокирование `SendInput` во всех 18 регрессионных тестах маскирует системные отказы ввода.
  Все тесты ввода в `test_regressions_audit.py` перехватывают вызов `actuator.user32.SendInput = fake_send`. Это отлично проверяет структуры в памяти, но не защищает от сбоев в реальной Win32-среде. Из-за этого тест-сьют в отчёте кодера выглядел «полностью зелёным», в то время как живой тест `test_click_reaches_element` в `test_probe.py` реально падал.
- **Что исправить кодеру**:
  Добавить интеграционный тест отправки нативного ввода на живом окне `Target` с проверкой возврата реальных событий через `clicks()`.

---

## 2. Разбор ответов и аргументов Кодера из `ОТВЕТ_НА_АУДИТ_09_v1.md`

1. **По символам вне BMP (`ord('🚀') = 0x1F680`)**:
   - Наблюдение кодера абсолютно точное: `ctypes` не выбрасывал `OverflowError`, а молча усекал 32-битное число до 16 бит (`0xF680`), тихо искажая вводимый символ. Замена на UTF-16 суррогатные пары полностью закрыла проблему.
2. **По `Job Object` против `taskkill`**:
   - Аргумент кодера о том, что `WINWORD.EXE` создается `rpcss` и не является прямым дочерним процессом воркера, принят. Действительно, чтобы Word попал в Job Object, его нужно явно добавлять через `AssignProcessToJobObject`.
   - Использование `taskkill` допустимо, но при двух обязательных условиях:
     а) наличие фильтра `/FI "IMAGENAME eq WINWORD.EXE"`;
     б) передача PID родителю до блокирующего вызова открытия файла (см. дефект Major выше).
3. **По `ScreenCaptureError` и `tests/conftest.py`**:
   - Добавление фикстуры `require_capture` в `conftest.py` и типизированного исключения `ScreenCaptureError` одобряется: теперь отказы графической подсистемы не маскируются под логические ошибки в коде.

---

## 3. Чеклист обязательных правок для следующей итерации (v3)

- [ ] **[P0]** Исправить `ScreenGrabber.covers` (`mcp_hands/screen.py`): проверять пересечение с областью кадра вместо строгого включения, чтобы не падать на максимизированных окнах с рамками -8 px.
- [ ] **[P0]** Добавить в `probe_worker.py` (`Worker.__init__`) и `mcp_hands/input.py` (`Actuator`) синхронизацию с активным рабочим столом ввода через `OpenInputDesktop` + `SetThreadDesktop`.
- [ ] **[P1]** Исправить `Actuator.click` (`mcp_hands/input.py`): не производить слепой клик при `cursor_available == False`, отправлять координаты с `MOUSEEVENTF_ABSOLUTE`.
- [ ] **[P1]** Обеспечить регистрацию PID Word в `ProbeProcess._tracked` до выполнения блокирующих вызовов `Documents.Open` в шаге 4.
- [ ] **[P1]** Добавить флаг безопасности `/FI "IMAGENAME eq ..."` в вызовы `taskkill` в `ProbeProcess.kill_tracked`.
- [ ] **[P2]** Добавить тест на максимизированное окно в `tests/test_regressions_audit.py`.
