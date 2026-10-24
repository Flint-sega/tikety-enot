"""Скорость загрузки: сжатие и кеш статики, HTML без сжатия и без кеша (BREACH, приватность)."""


def test_static_is_gzipped_and_cached(client, ui):
    response = client.get("/static/tailwind.css?v=1", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"
    assert "immutable" in response.headers["cache-control"]
    plain = client.get("/static/raccoon-avatar.webp")
    assert plain.headers["cache-control"] == "public, max-age=86400"


def test_html_not_compressed_and_not_cached(client, ui):
    response = client.get("/login", headers={"Accept-Encoding": "gzip"})
    assert response.headers.get("content-encoding") is None, "HTML с CSRF не сжимаем (BREACH)"
    assert response.headers["cache-control"] == "no-store"
