from fstec_monitor.normalize import normalize_document_html, normalize_extracted_text


def test_normalization_ignores_script_and_keeps_table():
    html="""<html><body><nav>x</nav><article><h1>Документ</h1><p>Пункт&nbsp; 1</p><table><tr><td>A</td><td>B</td></tr></table><script>noise</script></article></body></html>"""
    normalized,text=normalize_document_html(html)
    assert "noise" not in normalized
    assert "Пункт 1" in text
    assert "A | B" in text


def test_normalization_excludes_dropfiles_and_page_counters():
    html = """<main><article class='item-page'>
      <div class='com-content-article__info'>Просмотры: 10</div>
      <div class='dropfiles-content'>PDF Скачать 182 КБ</div>
      <div class='com-content-article__body'><h1>Требования</h1><p>Пункт 1.</p></div>
    </article></main>"""
    _, text = normalize_document_html(html)
    assert "Пункт 1." in text
    assert "Скачать" not in text
    assert "Просмотры" not in text


def test_normalization_collapses_formatting_noise_and_canonicalizes_dash_variants():
    noisy_html = """<main><article><h1>Требо­вания</h1>
      <p>Применяется&nbsp;между\nстроками</p><p>12‑месячный срок</p></article></main>"""
    clean_html = """<main><article><h1>Требования</h1>
      <p>Применяется между строками</p><p>12-месячный срок</p></article></main>"""

    noisy_markup, noisy_text = normalize_document_html(noisy_html)
    clean_markup, clean_text = normalize_document_html(clean_html)

    assert noisy_text == clean_text
    assert noisy_markup == clean_markup
    assert "А-Б" in normalize_document_html("<main><article><p>А–Б</p></article></main>")[1]
    assert "А-Б" in normalize_document_html("<main><article><p>А—Б</p></article></main>")[1]
    assert "А-Б" in normalize_document_html("<main><article><p>А‐Б</p></article></main>")[1]


def test_extracted_text_ignores_line_wrapping_and_preserves_paragraphs():
    assert normalize_extracted_text("Первый\nперенесённый абзац\n\nВторой абзац") == (
        "Первый перенесённый абзац\n\nВторой абзац"
    )
    assert normalize_extracted_text("Пункт 12‑й") == "Пункт 12-й"
    assert normalize_extracted_text("А–Б и А—Б") == "А-Б и А-Б"


def test_mass_typographic_dash_substitutions_do_not_hide_lexical_changes():
    from difflib import unified_diff

    old_text = "\n".join(
        [f"Пункт {number} - формулировка остается прежней" for number in range(1, 21)]
        + ["Пункт 21 - срок ежегодный"]
    )
    new_text = "\n".join(
        [f"Пункт {number} — формулировка остается прежней" for number in range(1, 21)]
        + ["Пункт 21 — срок ежемесячный"]
    )
    old_semantic = normalize_extracted_text(old_text)
    new_semantic = normalize_extracted_text(new_text)
    diff = list(unified_diff(old_semantic.splitlines(), new_semantic.splitlines(), lineterm=""))
    changed_lines = [line for line in diff if line.startswith(("-", "+")) and not line.startswith(("---", "+++"))]

    assert old_semantic.replace("срок ежегодный", "срок ежемесячный") == new_semantic
    assert len(changed_lines) == 2
    assert "срок ежегодный" in changed_lines[0]
    assert "срок ежемесячный" in changed_lines[1]
    assert all(" — " not in line for line in changed_lines)
