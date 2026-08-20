from fstec_monitor.telegram.navigation import NavigationStack, Screen, main_screen


def test_navigation_stack_back_restores_previous_screen() -> None:
    stack = NavigationStack()
    stack.reset("main")
    stack.push("settings")
    stack.push("filters")

    assert stack.current == "filters"
    assert stack.back() == "settings"
    assert stack.back() == "main"
    assert stack.back() == "main"


def test_main_screen_uses_inline_buttons_for_single_screen_ui() -> None:
    screen = main_screen(is_admin=True)

    assert isinstance(screen, Screen)
    assert "inline_keyboard" in screen.markup
    assert "keyboard" not in screen.markup
    assert "🔍 Проверить сейчас" in str(screen.markup)


def test_navigation_reset_discards_stale_screen_stack() -> None:
    stack = NavigationStack()
    stack.push("main")
    stack.push("settings")
    stack.reset("main")

    assert stack.current == "main"
    assert stack.back() == "main"
