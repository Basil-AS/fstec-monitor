from fstec_monitor.normalize import normalize_document_html

def test_normalization_ignores_script_and_keeps_table():
    html="""<html><body><nav>x</nav><article><h1>Документ</h1><p>Пункт&nbsp; 1</p><table><tr><td>A</td><td>B</td></tr></table><script>noise</script></article></body></html>"""
    normalized,text=normalize_document_html(html)
    assert "noise" not in normalized
    assert "Пункт 1" in text
    assert "A | B" in text
