# TV M3U Bot

Türkiye/Türkçe açık yayın listelerini düzenli olarak kontrol eder, tekrarları temizler ve tek M3U dosyasında toplar.

## Kısa liste

GitHub Pages etkinleştirildikten sonra:

`https://kodlama155-ctrl.github.io/tv/a.m3u`

## Dosyalar

- `sources.txt` — kaynak M3U listeleri
- `checker.py` — kontrol/ayıklama botu
- `a.m3u` — EmirTV için üretilen liste
- `stats.json` — son çalışma bilgileri
- `.github/workflows/update.yml` — 6 saatte bir otomatik güncelleme

Bot yalnızca açıkça sağlanan kaynak listelerini işler; DRM atlatma veya erişim kontrolü aşma yapmaz.
