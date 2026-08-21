from __future__ import annotations

from dataclasses import dataclass, field

from .callbacks import encode_callback


@dataclass(frozen=True)
class Screen:
    name: str
    text: str
    markup: dict


@dataclass
class NavigationStack:
    _screens: list[tuple[str, object | None]] = field(default_factory=list)

    @property
    def current(self) -> str:
        return self._screens[-1][0] if self._screens else "main"

    @property
    def current_payload(self) -> object | None:
        return self._screens[-1][1] if self._screens else None

    def reset(self, screen: str = "main") -> None:
        self._screens[:] = [(screen, None)]

    def push(self, screen: str, payload: object | None = None) -> None:
        if not self._screens or self.current != screen or self.current_payload != payload:
            self._screens.append((screen, payload))

    def replace(self, screen: str, payload: object | None = None) -> None:
        if self._screens:
            self._screens[-1] = (screen, payload)
        else:
            self.reset(screen)

    def back(self) -> str:
        if len(self._screens) > 1:
            self._screens.pop()
        return self.current

    def back_frame(self) -> tuple[str, object | None]:
        self.back()
        return self.current, self.current_payload


def _button(text: str, section: str, action: str) -> dict:
    return {"text": text, "callback_data": encode_callback(section, action)}


def _navigation_row() -> list[dict]:
    return [_button("🏠 Главное меню", "menu", "main")]


def main_screen(*, is_admin: bool) -> Screen:
    if is_admin:
        rows = [
            [_button("📊 Статус", "screen", "status"), _button("📰 Изменения", "screen", "changes")],
            [_button("🔍 Проверить сейчас", "screen", "scan"), _button("⚙️ Настройки", "screen", "settings")],
            [_button("🚫 Игнор категорий", "screen", "filters"), _button("👥 Пользователи", "screen", "users")],
            [_button("🧯 Ошибки", "screen", "errors"), _button("ℹ️ Помощь", "screen", "help")],
        ]
        title = "🛡 <b>ФСТЭК Monitor</b>\n\nПанель администратора\nВыберите раздел:"
    else:
        rows = [
            [_button("📰 Последние изменения", "screen", "changes")],
            [_button("🚫 Мои категории", "screen", "my_ignore")],
            [_button("ℹ️ Помощь", "screen", "help")],
        ]
        title = "🛡 <b>ФСТЭК Monitor</b>\n\nЛента обновлений\nВыберите раздел:"
    return Screen("main", title, {"inline_keyboard": rows})


def back_row() -> list[list[dict]]:
    return [[_button("← Назад", "nav", "back"), *_navigation_row()]]


def screen_with_navigation(name: str, text: str, rows: list[list[dict]] | None = None) -> Screen:
    return Screen(name, text, {"inline_keyboard": [*(rows or []), *back_row()]})
