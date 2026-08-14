from fstec_monitor.http import conditional_headers
from fstec_monitor.parser import is_document_url, parse_page


def test_parser_distinguishes_documents_and_files():
    html="""<html><body><main><h1>Каталог</h1><a href='/dokumenty/vse-dokumenty/prikazy/doc-1'>Doc</a><a href='/files/a.pdf'>PDF</a><a href='/files/a.odt'>ODT</a></main></body></html>"""
    p=parse_page(html,"https://fstec.ru/dokumenty/vse-dokumenty","https://fstec.ru/dokumenty/vse-dokumenty")
    assert len(p.document_links)==1
    assert {x.url.rsplit('.',1)[-1] for x in p.attachments}=={"pdf","odt"}


def test_document_url_is_deeper_than_category_url():
    prefix = "https://fstec.ru/dokumenty/vse-dokumenty"
    assert not is_document_url(prefix + "/prikazy", prefix)
    assert is_document_url(prefix + "/prikazy/doc-1", prefix)


def test_conditional_headers_only_include_available_validators():
    assert conditional_headers('"abc"', "Wed, 01 Jan 2025 00:00:00 GMT") == {
        "If-None-Match": '"abc"',
        "If-Modified-Since": "Wed, 01 Jan 2025 00:00:00 GMT",
    }
    assert conditional_headers() == {}
