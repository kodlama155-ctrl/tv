# TV M3U Bot

Türkiye/Türkçe açık yayın listelerini toplar, yeni açık GitHub kaynaklarını keşfeder ve HLS yayınlarını gerçek medya segmentlerine kadar doğrular.

## Listeler

- `a.m3u` — **ana liste**; yalnızca HLS manifesti + kalite varyantı + gerçek medya segment testi geçen yayınlar
- `r.m3u` — GitHub sunucusundan 401/403/451 dönen, bölgesel/erişim kısıtlı olabilecek yayınlar
- `u.m3u` — timeout, 429, geçici CDN hatası veya segment doğrulaması belirsiz yayınlar
- `all.m3u` — verified + restricted + unknown birlikte
- `validation.json` — kanal bazında doğrulama sonucu, neden, çözünürlük ve gecikme bilgileri
- `stats.json` — son çalışma özeti

## Ana M3U

Raw GitHub:

`https://raw.githubusercontent.com/kodlama155-ctrl/tv/main/a.m3u`

GitHub Pages etkinleştirildikten sonra kısa adres:

`https://kodlama155-ctrl.github.io/tv/a.m3u`

## Doğrulama mantığı

Bot yalnızca HTTP 200 cevabına bakmaz. Ana HLS playlistini açar, master playlist ise en iyi varyantlardan birine girer, AES-128 anahtarı veya fMP4 init segmenti gerekiyorsa erişimi kontrol eder ve mümkünse iki gerçek medya segmentini indirip medya içeriği olduğunu doğrular.

DRM/SAMPLE-AES yayınlar ana listeye alınmaz. Kesin 404/410 yayınlar ölü kabul edilir. Türkiye dışındaki GitHub Actions sunucusunda 403 alan yayınlar doğrudan silinmez; `r.m3u` listesine ayrılır.

## Otomasyon

GitHub Actions her 6 saatte bir:

1. Açık GitHub repo/listelerinde yeni M3U8 adayları arar.
2. Kaynak listeleri birleştirip tekrar eden URL'leri temizler.
3. Gerçek HLS segment testi yapar.
4. Kategorileri Türkçeleştirir ve düzenler.
5. Çıktı listelerini otomatik commit eder.

Bot DRM atlatma, giriş/abonelik aşma veya erişim kontrolü kırma yapmaz.
