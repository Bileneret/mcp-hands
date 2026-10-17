"""Патч по итогам аудита v6: живой сторож модалок, бюджеты, утечка Quit, реестр, пути."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def patch(relative: str, replacements: list[tuple[str, str]], note: str) -> None:
    path = ROOT / relative
    text = path.read_text(encoding="utf-8")
    for old, new in replacements:
        if old not in text:
            raise SystemExit(f"не найден фрагмент в {relative}: {old[:70]!r}")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")
    print(f"{relative}: {note}")


# ---------------- 1. ProbeProcess: вызов с опросом во время ожидания ----------------
patch("mcp_hands/probe.py", [
    ('''class ProbeError(RuntimeError):
    """Операция завершилась ошибкой внутри сателлита (не таймаут)."""''',
     '''class ProbeAborted(RuntimeError):
    """Вызов прерван наблюдателем: сателлит снят, пока операция ещё выполнялась.

    Так работает слежение за модальными окнами: пока COM-вызов идёт в сателлите,
    родитель обязан что-то проверять, а не спать в ожидании ответа.
    """


class ProbeError(RuntimeError):
    """Операция завершилась ошибкой внутри сателлита (не таймаут)."""'''),
    ('''    def call(self, op: str, timeout_s: float | None = None, **kwargs) -> object:
        """Выполняет операцию в сателлите. Таймаут — убийство процесса, а не ожидание."""
        budget = timeout_s if timeout_s is not None else self.default_timeout_s
        if not self.alive:
            self.start()
        started = time.perf_counter()
        try:
            self._connection.send({"op": op, "kwargs": kwargs})
        except (BrokenPipeError, OSError, AttributeError) as exc:
            self.stats.errors += 1
            self.kill()
            raise ProbeError(f"канал сателлита недоступен: {exc}") from exc

        if not self._connection.poll(budget):
            elapsed = (time.perf_counter() - started) * 1000.0
            self.stats.timeouts += 1
            self.stats.record(op, elapsed, "timeout")
            self.kill()
            raise ProbeTimeout(
                f"операция {op!r} не ответила за {budget:.2f} с — сателлит снят и будет поднят заново")''',
     '''    def call(self, op: str, timeout_s: float | None = None, on_tick=None,
             tick_s: float = 0.1, **kwargs) -> object:
        """Выполняет операцию в сателлите. Таймаут — убийство процесса, а не ожидание.

        `on_tick` вызывается каждые `tick_s` секунд, ПОКА операция идёт. Если вернёт
        непустое значение — вызов прерывается, сателлит снимается, а наружу уходит
        `ProbeAborted` с причиной. Так наблюдают за модальными окнами: ждать ответа
        молча нельзя, иначе сторож узнаёт о препятствии только по истечении бюджета.
        """
        budget = timeout_s if timeout_s is not None else self.default_timeout_s
        if not self.alive:
            self.start()
        started = time.perf_counter()
        try:
            self._connection.send({"op": op, "kwargs": kwargs})
        except (BrokenPipeError, OSError, AttributeError) as exc:
            self.stats.errors += 1
            self.kill()
            raise ProbeError(f"канал сателлита недоступен: {exc}") from exc

        while True:
            elapsed = time.perf_counter() - started
            remaining = budget - elapsed
            if remaining <= 0:
                self.stats.timeouts += 1
                self.stats.record(op, elapsed * 1000.0, "timeout")
                self.kill()
                raise ProbeTimeout(
                    f"операция {op!r} не ответила за {budget:.2f} с — сателлит снят "
                    f"и будет поднят заново")
            if not self._connection.poll(min(tick_s, remaining)):
                if on_tick is not None:
                    reason = on_tick()
                    if reason:
                        self.stats.record(op, elapsed * 1000.0, "aborted")
                        self.kill()
                        raise ProbeAborted(str(reason))
                continue
            break'''),
    ('''    def register_external(self, pid: int, image: str = "", note: str = "") -> None:''',
     '''    def register_external(self, pid: int, image: str = "", note: str = "",
                          created: int | None = None) -> None:'''),
    ('''        if pid and pid > 0 and pid not in self._tracked:
            self._tracked[pid] = {"note": note or "из реестра сателлита", "image": image}''',
     '''        if pid and pid > 0 and pid not in self._tracked:
            # Время создания обязательно переносим из реестра: без него отключается
            # защита от убийства процесса с переиспользованным номером.
            self._tracked[pid] = {"note": note or "из реестра сателлита", "image": image,
                                  "created": created if created is not None
                                  else self.process_identity(pid)["created"]}'''),
    ('''            if pid and pid not in self._tracked:
                self.register_external(pid, entry.get("image", ""), entry.get("note", ""))
                added.append(entry)''',
     '''            if pid and pid not in self._tracked:
                self.register_external(pid, entry.get("image", ""), entry.get("note", ""),
                                       entry.get("created"))
                added.append(entry)'''),
], "вызов с опросом, сохранение времени создания из реестра")

# ---------------- 2. office.py: живой сторож, бюджеты по операциям, пути, quit ----------------
patch("mcp_hands/office.py", [
    ('''#: Бюджет COM-операции. Запуск Word и экспорт PDF занимают секунды, поэтому общий
#: бюджет прикладных операций здесь не годится. Верхняя граница задана сознательно
#: большой: зависший на модальном диалоге вызов всё равно будет снят по этому бюджету,
#: а разумность значения определяется замерами сценария.
OFFICE_OPERATION_TIMEOUT_S = 90.0''',
     '''#: Бюджеты COM-операций подобраны по замерам на стенде и различаются по цене:
#: спросить состояние — доли секунды, поднять или закрыть Word — секунды,
#: открыть документ и напечатать PDF — десятки секунд. Единый бюджет здесь вреден:
#: слишком большой держит зависший вызов, слишком малый срывает долгую печать.
OFFICE_TIMEOUTS = {
    "word_start": 30.0,
    "word_quit": 30.0,
    "word_status": 5.0,
    "word_new_document": 30.0,
    "word_open": 45.0,
    "word_replace": 45.0,
    "word_save_as": 60.0,
}
DEFAULT_OFFICE_TIMEOUT_S = 45.0


def timeout_for(op: str) -> float:
    return OFFICE_TIMEOUTS.get(op, DEFAULT_OFFICE_TIMEOUT_S)'''),
    ('''    def _call(self, op: str, timeout_s: float = OFFICE_OPERATION_TIMEOUT_S,
              **kwargs) -> OfficeOutcome:
        """Вызов COM-операции с повторами на занятость сервера и слежением за модалками."""
        started = time.perf_counter()
        attempts = 0
        last_reason = ""
        while attempts < 3:
            attempts += 1
            if self.modal_watcher is not None:
                self.modal_watcher.check(word_pid=self.pid)
            try:
                value = self.probe.call(op, timeout_s=timeout_s, **kwargs)
                outcome = OfficeOutcome(True, value=value, attempts=attempts,
                                        ms=(time.perf_counter() - started) * 1000.0)
                self._remember(op, outcome)
                return outcome
            except ProbeTimeout as exc:
                outcome = OfficeOutcome(False, reason=self._timeout_reason(exc), attempts=attempts,
                                        ms=(time.perf_counter() - started) * 1000.0)
                self._remember(op, outcome)
                return outcome
            except ProbeError as exc:
                last_reason = str(exc)
                if self._is_busy(last_reason) and attempts < 3:
                    time.sleep(0.1 * attempts)  # Word сам просит повторить
                    continue
                outcome = OfficeOutcome(False, reason=last_reason, attempts=attempts,
                                        ms=(time.perf_counter() - started) * 1000.0)
                if self.modal_watcher is not None:
                    modal = self.modal_watcher.recent(word_pid=self.pid)
                    if modal:
                        outcome.modal_dialog = modal
                        outcome.reason = f"модальный диалог Word ({modal}): нужен человек"
                self._remember(op, outcome)
                return outcome
        return OfficeOutcome(False, reason=last_reason or "операция не удалась", attempts=attempts)''',
     '''    def _call(self, op: str, timeout_s: float | None = None, **kwargs) -> OfficeOutcome:
        """Вызов COM-операции с повторами на занятость сервера и ЖИВЫМ сторожем модалок.

        Сторож опрашивается, пока операция выполняется: `ProbeProcess.call` умеет звать
        наблюдателя между короткими ожиданиями. Иначе модальное окно Word обнаружилось бы
        только по истечении бюджета, да ещё и без имени диалога.
        """
        budget = timeout_s if timeout_s is not None else timeout_for(op)
        started = time.perf_counter()
        attempts = 0
        last_reason = ""
        while attempts < 3:
            attempts += 1
            guard = None
            if self.modal_watcher is not None:
                guard = lambda: self._modal_reason()  # noqa: E731 — короткая обёртка для колбэка
            try:
                value = self.probe.call(op, timeout_s=budget, on_tick=guard, **kwargs)
                outcome = OfficeOutcome(True, value=value, attempts=attempts,
                                        ms=(time.perf_counter() - started) * 1000.0)
                self._remember(op, outcome)
                return outcome
            except ProbeAborted as exc:
                outcome = OfficeOutcome(False, reason=str(exc), attempts=attempts,
                                        modal_dialog=self._modal_reason(),
                                        ms=(time.perf_counter() - started) * 1000.0)
                outcome.details["budget_s"] = budget
                self._remember(op, outcome)
                return outcome
            except ProbeTimeout as exc:
                outcome = OfficeOutcome(False, reason=self._timeout_reason(exc), attempts=attempts,
                                        ms=(time.perf_counter() - started) * 1000.0)
                # Модальное окно могло появиться в последний момент: опрашиваем сторожа
                # и в этой ветке, иначе причина отказа останется необъяснённой.
                outcome.modal_dialog = self._modal_reason()
                if outcome.modal_dialog:
                    outcome.reason = (f"модальный диалог Word ({outcome.modal_dialog}): "
                                      f"нужен человек")
                outcome.details["budget_s"] = budget
                self._remember(op, outcome)
                return outcome
            except ProbeError as exc:
                last_reason = str(exc)
                if self._is_busy(last_reason) and attempts < 3:
                    time.sleep(0.1 * attempts)  # Word сам просит повторить
                    continue
                outcome = OfficeOutcome(False, reason=last_reason, attempts=attempts,
                                        ms=(time.perf_counter() - started) * 1000.0)
                outcome.modal_dialog = self._modal_reason()
                if outcome.modal_dialog:
                    outcome.reason = (f"модальный диалог Word ({outcome.modal_dialog}): "
                                      f"нужен человек")
                self._remember(op, outcome)
                return outcome
        return OfficeOutcome(False, reason=last_reason or "операция не удалась", attempts=attempts)

    def _modal_reason(self) -> str:
        """Причина остановки, если Word показывает модальное окно, иначе пусто."""
        if self.modal_watcher is None:
            return ""
        modal = self.modal_watcher.recent(word_pid=self.pid)
        return f"модальный диалог Word ({modal}): нужен человек" if modal else ""'''),
    ('''    def start(self, visible: bool = False) -> OfficeOutcome:
        """Поднимает Word и берёт его под опеку супервизора ДО первой блокирующей операции."""
        outcome = self._call("word_start", visible=visible)''',
     '''    def start(self, visible: bool = False) -> OfficeOutcome:
        """Поднимает Word и берёт его под опеку супервизора ДО первой блокирующей операции."""
        outcome = self._call("word_start", visible=visible)'''),
    ('''    def open(self, path: str) -> OfficeOutcome:
        """Открывает документ. Самый опасный вызов: здесь чаще всего всплывают модалки."""
        return self._call("word_open", path=str(path))''',
     '''    def open(self, path: str) -> OfficeOutcome:
        """Открывает документ. Самый опасный вызов: здесь чаще всего всплывают модалки."""
        return self._call("word_open", path=_absolute(path))'''),
    ('''    def save_as(self, path: str, as_pdf: bool = False) -> OfficeOutcome:
        return self._call("word_save_as", path=str(path), as_pdf=as_pdf)

    def export_pdf(self, path: str) -> OfficeOutcome:
        """Экспорт в PDF. Word на этой операции часто отвечает «сервер занят» — повтор встроен."""
        return self._call("word_save_as", path=str(path), as_pdf=True)''',
     '''    def save_as(self, path: str, as_pdf: bool = False) -> OfficeOutcome:
        return self._call("word_save_as", path=_absolute(path), as_pdf=as_pdf)

    def export_pdf(self, path: str) -> OfficeOutcome:
        """Экспорт в PDF. Word на этой операции часто отвечает «сервер занят» — повтор встроен."""
        return self._call("word_save_as", path=_absolute(path), as_pdf=True)'''),
    ('''    def quit(self, save: bool = False) -> OfficeOutcome:
        """Закрывает Word и снимает его с учёта."""
        outcome = self._call("word_quit", save=save)
        if self.pid:
            self.probe.untrack_process(self.pid)
            try:
                self.probe.call("unregister_process", pid=self.pid,
                                timeout_s=DOMAIN_OPERATION_TIMEOUT_S)
            except (ProbeError, ProbeTimeout):
                pass  # сателлит мог быть снят; запись всё равно уберётся при его перезапуске
            self.pid = 0
        return outcome''',
     '''    def quit(self, save: bool = False) -> OfficeOutcome:
        """Закрывает Word и снимает его с учёта.

        Если штатное закрытие не удалось (Word занят, отвечает ошибкой), процесс
        добивается принудительно: он уже под опекой, и «не закрылся» не должно означать
        «остался висеть». Результат проверяется фактом — исчез ли процесс.
        """
        outcome = self._call("word_quit", save=save)
        pid = self.pid
        if pid:
            if not outcome.ok:
                killed = self.probe.kill_tracked()
                outcome.details["forced_kill"] = killed
                outcome.reason = (outcome.reason or "закрытие не подтвердилось") + \\
                    f"; Word снят принудительно ({killed})"
            gone = not self.probe.process_identity(pid)["image"]
            outcome.details["process_gone"] = gone
            if not gone and outcome.ok:
                outcome.ok = False
                outcome.reason = f"Word с pid {pid} продолжает работать после закрытия"
            self.probe.untrack_process(pid)
            try:
                self.probe.call("unregister_process", pid=pid,
                                timeout_s=DOMAIN_OPERATION_TIMEOUT_S)
            except (ProbeError, ProbeTimeout):
                pass  # сателлит мог быть снят; запись всё равно уберётся при его перезапуске
            self.pid = 0
        return outcome'''),
    ('''from .probe import DOMAIN_OPERATION_TIMEOUT_S, ProbeError, ProbeProcess, ProbeTimeout''',
     '''from pathlib import Path

from .probe import (DOMAIN_OPERATION_TIMEOUT_S, ProbeAborted, ProbeError, ProbeProcess,
                    ProbeTimeout)


def _absolute(path: str) -> str:
    """Приводит путь к абсолютному: относительный Word понимает по-своему.

    Word сохраняет и ищет файлы относительно своей «папки документов», а не рабочего
    каталога вызывающего процесса: без приведения пути файл окажется не там, где ждут,
    и проверка постусловия не найдёт результат.
    """
    return str(Path(path).expanduser().resolve())'''),
], "живой сторож модалок, бюджеты по операциям, абсолютные пути, проверка закрытия")

# ---------------- 3. probe_worker: Quit без потери ссылки ----------------
patch("mcp_hands/probe_worker.py", [
    ('''    def op_word_quit(self, save: bool = False) -> dict:
        """Закрывает Word и снимает pid с учёта."""
        if self._word is None:
            return {"closed": True, "already": True}
        pid = self._word_pid()
        try:
            self._word.Quit(0 if not save else -1)  # 0 = wdDoNotSaveChanges
        finally:
            self._word = None
        if pid:
            self.op_unregister_process(pid=pid)
        return {"closed": True, "pid": pid}''',
     '''    def op_word_quit(self, save: bool = False) -> dict:
        """Закрывает Word и снимает pid с учёта.

        Ссылка на COM-объект обнуляется ТОЛЬКО после успешного `Quit`. Иначе повтор
        (на занятость сервера Word отвечает ошибкой, и вызывающий повторяет) увидел бы
        `self._word is None` и отчитался фиктивным успехом — процесс остался бы висеть.
        """
        if self._word is None:
            return {"closed": True, "already": True}
        pid = self._word_pid()
        self._word.Quit(0 if not save else -1)  # 0 = wdDoNotSaveChanges
        self._word = None
        if pid:
            self.op_unregister_process(pid=pid)
        return {"closed": True, "pid": pid}'''),
], "повтор Quit видит тот же объект Word")
