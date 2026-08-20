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
    _screens: list[str] = field(default_factory=list)

    @property
    def current(self) -> str:
        return self._screens[-1] if self._screens else "main"

    def reset(self, screen: str = "main") -> None:
        self._screens[:] = [screen]

    def push(self, screen: str, payload: object | None = None) -> None:
        del payload
        if self.current != screen:
            self._screens.append(screen)

    def back(self) -> str:
        if len(self._screens) > 1:
            self._screens.pop()
        return self.current


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
