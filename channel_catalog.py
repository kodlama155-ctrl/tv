#!/usr/bin/env python3
from __future__ import annotations

import re
import unicodedata

# EmirTV authoritative channel/category catalog.
#
# Category placement is owned by this file. External platforms may still be
# used for discovery and ordering, but they must not move a known channel to a
# different category. Unknown channels intentionally fall back to "Diğer" so
# they can be reviewed before being promoted into a curated category.

CATEGORY_ORDER = [
    "Ulusal",
    "Haber",
    "Spor",
    "Film & Dizi",
    "Çocuk",
    "Belgesel",
    "Dini",
    "Müzik",
    "Yerel",
    "Uluslararası",
    "Diğer",
]

CHANNELS = [
    {"id": "TRT1.tr@SD", "name": "TRT 1", "category": "Ulusal"},
    {"id": "", "name": "Kanal D", "category": "Ulusal"},
    {"id": "ATV.tr@HD", "name": "ATV", "category": "Ulusal"},
    {"id": "", "name": "Show TV", "category": "Ulusal"},
    {"id": "", "name": "Star TV", "category": "Ulusal"},
    {"id": "NOWTV.tr@SD", "name": "NOW TV", "category": "Ulusal"},
    {"id": "", "name": "TV8", "category": "Ulusal"},
    {"id": "", "name": "Kanal 7", "category": "Ulusal"},
    {"id": "A2TV.tr@SD", "name": "A2", "category": "Ulusal"},
    {"id": "CNBCe.tr@SD", "name": "CNBC-e", "category": "Ulusal"},
    {"id": "BeyazTV.tr@HD", "name": "Beyaz TV", "category": "Ulusal"},
    {"id": "Teve2.tr@HD", "name": "Teve2", "category": "Ulusal"},
    {"id": "TV85.tr@HD", "name": "TV8.5", "category": "Ulusal"},
    {"id": "TRT2.tr@HD", "name": "TRT 2", "category": "Ulusal"},
    {"id": "360.tr@SD", "name": "360", "category": "Ulusal"},
    {"id": "TV100.tr@HD", "name": "TV100", "category": "Haber"},
    {"id": "NTV.tr@SD", "name": "NTV", "category": "Haber"},
    {"id": "CNNTurk.tr@HD", "name": "CNN Türk", "category": "Haber"},
    {"id": "", "name": "TRT Haber", "category": "Haber"},
    {"id": "AHaber.tr@HD", "name": "A Haber", "category": "Haber"},
    {"id": "", "name": "Habertürk", "category": "Haber"},
    {"id": "24TV.tr@SD", "name": "24 TV", "category": "Haber"},
    {"id": "BloombergHT.tr@SD", "name": "Bloomberg HT", "category": "Haber"},
    {"id": "APara.tr@HD", "name": "A Para", "category": "Haber"},
    {"id": "UlkeTV.tr@HD", "name": "Ülke TV", "category": "Haber"},
    {"id": "HaberGlobal.tr@SD", "name": "Haber Global", "category": "Haber"},
    {"id": "TGRTHaber.tr@HD", "name": "TGRT Haber", "category": "Haber"},
    {"id": "", "name": "TVNET", "category": "Haber"},
    {"id": "AkitTV.tr@HD", "name": "AKİT TV", "category": "Haber"},
    {"id": "FlashHaberTV.tr@SD", "name": "Flash Haber TV", "category": "Haber"},
    {"id": "ASTV.tr@SD", "name": "AS TV", "category": "Haber"},
    {"id": "EkolTV.tr@HD", "name": "EKOL TV", "category": "Haber"},
    {"id": "TurkHaberTV.tr@SD", "name": "TurkHaber TV", "category": "Haber"},
    {"id": "HalkTV.tr@SD", "name": "Halk TV", "category": "Haber"},
    {"id": "", "name": "EKOTÜRK", "category": "Haber"},
    {"id": "BenguturkTV.tr@HD", "name": "BengüTürk", "category": "Haber"},
    {"id": "SozcuTV.tr@HD", "name": "Sözcü TV", "category": "Haber"},
    {"id": "ANews.tr@HD", "name": "A News", "category": "Haber"},
    {"id": "TRTWorld.tr@SD", "name": "TRT World", "category": "Uluslararası"},
    {"id": "TRTArabi.tr@SD", "name": "TRT ARABI", "category": "Uluslararası"},
    {"id": "AlmahriahTV.tr@SD", "name": "Almahriah TV", "category": "Haber"},
    {"id": "DHA.tr@SD", "name": "DHA", "category": "Haber"},
    {"id": "DimTV.tr@SD", "name": "DİM TV", "category": "Haber"},
    {"id": "FinansTurkTV.tr@SD", "name": "Finans Turk TV", "category": "Haber"},
    {"id": "GuneydoguTV.tr@SD", "name": "Guneydogu TV", "category": "Haber"},
    {"id": "Haber61TV.tr@SD", "name": "Haber61 TV", "category": "Haber"},
    {"id": "KudusTV.tr@SD", "name": "Kudüs TV", "category": "Haber"},
    {"id": "LifeTV.tr@SD", "name": "Life TV", "category": "Haber"},
    {"id": "MekameleenTV.tr@SD", "name": "Mekameleen TV", "category": "Haber"},
    {"id": "OlayTurkTV.tr@SD", "name": "OlayTurk TV", "category": "Haber"},
    {"id": "", "name": "TRT Spor", "category": "Spor"},
    {"id": "ASpor.tr@HD", "name": "A Spor", "category": "Spor"},
    {"id": "TRTSporYildiz.tr@SD", "name": "TRT Spor Yıldız", "category": "Spor"},
    {"id": "FBTV.tr@SD", "name": "FB TV", "category": "Spor"},
    {"id": "TRT3.tr@SD", "name": "TRT 3 Spor", "category": "Spor"},
    {"id": "HTSporTV.tr@SD", "name": "HT Spor", "category": "Spor"},
    {"id": "beINSportsHaber.tr@HD", "name": "beIN Sports Haber", "category": "Spor"},
    {"id": "EkolSports.tr@HD", "name": "Ekol Sports", "category": "Spor"},
    {"id": "TJKTV.tr@SD", "name": "TJK TV", "category": "Spor"},
    {"id": "FX.tr@HD", "name": "FX", "category": "Film & Dizi"},
    {"id": "BBCFirst.uk@Turkiye", "name": "like Gecko) Chrome/120.0.0.0 Safari/537.36\" group-title=\"Film & Dizi\",BBC First", "category": "Film & Dizi"},
    {"id": "GEMPixel.tr@SD", "name": "GEM Pixel", "category": "Film & Dizi"},
    {"id": "GrandCinema.tr@SD", "name": "Grand Cinema", "category": "Film & Dizi"},
    {"id": "KanalDDrama.tr@SD", "name": "Kanal D Drama", "category": "Film & Dizi"},
    {"id": "DisneyJr.tr@SD", "name": "Disney Junior", "category": "Çocuk"},
    {"id": "BabyTV.uk@Turkiye", "name": "BabyTV", "category": "Çocuk"},
    {"id": "TRTCocuk.tr@SD", "name": "TRT Çocuk", "category": "Çocuk"},
    {"id": "MinikaCocuk.tr@SD", "name": "Minika Çocuk", "category": "Çocuk"},
    {"id": "MinikaGo.tr@SD", "name": "Minika GO", "category": "Çocuk"},
    {"id": "SpacetoonTurkey.tr@SD", "name": "Spacetoon Turkey", "category": "Çocuk"},
    {"id": "TRTGenc.tr@SD", "name": "TRT Genc", "category": "Çocuk"},
    {"id": "BabyFirst.us@US", "name": "BabyFirst", "category": "Çocuk"},
    {"id": "TRTBelgesel.tr@HD", "name": "TRT Belgesel", "category": "Belgesel"},
    {"id": "NationalGeographic.tr@SD", "name": "National Geographic", "category": "Belgesel"},
    {"id": "NationalGeographicWild.tr@SD", "name": "National Geographic Wild", "category": "Belgesel"},
    {"id": "LoveNature.ca@SD", "name": "Love Nature", "category": "Belgesel"},
    {"id": "HabitatTV.tr@HD", "name": "Habitat TV", "category": "Belgesel"},
    {"id": "CiftciTV.tr@SD", "name": "Ciftci TV", "category": "Belgesel"},
    {"id": "TRTEBA.tr@SD", "name": "TRT EBA", "category": "Belgesel"},
    {"id": "ManasTV.kg@SD", "name": "Manas TV", "category": "Belgesel"},
    {"id": "TGRTBelgesel.tr@HD", "name": "TGRT Belgesel", "category": "Belgesel"},
    {"id": "YabanTV.tr@HD", "name": "Yaban TV", "category": "Belgesel"},
    {"id": "", "name": "VAV TV", "category": "Dini"},
    {"id": "TRTDiyanetCocuk.tr@SD", "name": "TRT Diyanet Cocuk", "category": "Dini"},
    {"id": "DiyanetTV.tr@SD", "name": "Diyanet TV", "category": "Dini"},
    {"id": "AlZahraTVTurkic.tr@SD", "name": "Al-Zahra TV Turkic", "category": "Dini"},
    {"id": "DiyarTV.tr@SD", "name": "Diyar TV", "category": "Dini"},
    {"id": "DostTV.tr@SD", "name": "Dost TV", "category": "Dini"},
    {"id": "ImamHusseinTV5.iq@SD", "name": "Imam Hussein TV 5", "category": "Dini"},
    {"id": "LalegulTV.tr@SD", "name": "Lalegul TV", "category": "Dini"},
    {"id": "SercemTV.tr@HD", "name": "Sercem TV", "category": "Dini"},
    {"id": "DreamTurk.tr@SD", "name": "Dream Türk", "category": "Müzik"},
    {"id": "PowerTV.tr@SD", "name": "Power TV", "category": "Müzik"},
    {"id": "TRTMuzik.tr@SD", "name": "TRT Müzik", "category": "Müzik"},
    {"id": "PowerTurkTV.tr@SD", "name": "PowerTürk TV", "category": "Müzik"},
    {"id": "Number1TV.tr@SD", "name": "Number 1 TV", "category": "Müzik"},
    {"id": "KNMusicTV.az@SD", "name": "KN Music TV", "category": "Müzik"},
    {"id": "MedMuzik.tr@SD", "name": "Med Muzik", "category": "Müzik"},
    {"id": "Number1Ask.tr@SD", "name": "Number 1 Ask", "category": "Müzik"},
    {"id": "Number1Damar.tr@SD", "name": "Number 1 Damar", "category": "Müzik"},
    {"id": "Number1Dance.tr@SD", "name": "Number 1 Dance", "category": "Müzik"},
    {"id": "PowerDance.tr@SD", "name": "Power Dance", "category": "Müzik"},
    {"id": "PowerLove.tr@SD", "name": "Power Love", "category": "Müzik"},
    {"id": "PowerTurkAkustik.tr@SD", "name": "PowerTurk Akustik", "category": "Müzik"},
    {"id": "PowerTurkSlow.tr@SD", "name": "PowerTurk Slow", "category": "Müzik"},
    {"id": "PowerTurkTaptaze.tr@SD", "name": "PowerTurk Taptaze", "category": "Müzik"},
    {"id": "StingrayNaturescape.ca@SD", "name": "Stingray Naturescape", "category": "Müzik"},
    {"id": "AdaTV.cy@SD", "name": "Ada TV", "category": "Uluslararası"},
    {"id": "KurdistanTV.iq@SD", "name": "Kurdistan TV", "category": "Uluslararası"},
    {"id": "MCTV.de@HD", "name": "MC TV", "category": "Uluslararası"},
    {"id": "MyZenTV.fr@SD", "name": "MyZen TV", "category": "Uluslararası"},
    {"id": "PersianaTurkiye.fr@SD", "name": "Persiana Turkiye", "category": "Uluslararası"},
    {"id": "Sat7Turk.cy@SD", "name": "Sat 7 Turk", "category": "Uluslararası"},
    {"id": "WestAzerbaijanTV.ir@SD", "name": "West Azerbaijan TV", "category": "Uluslararası"},
    {"id": "YOLTV.de@SD", "name": "YOL TV", "category": "Uluslararası"},
    {"id": "TV4.tr@SD", "name": "TV4", "category": "Diğer"},
    {"id": "Tivi6.tr@SD", "name": "Tivi 6", "category": "Diğer"},
    {"id": "TRTTurk.tr@SD", "name": "TRT Türk", "category": "Diğer"},
    {"id": "TRTAvaz.tr@SD", "name": "TRT AVAZ", "category": "Diğer"},
    {"id": "TRTKurdi.tr@SD", "name": "TRT KURDİ", "category": "Diğer"},
    {"id": "SemerkandTV.tr@SD", "name": "Semerkand TV", "category": "Diğer"},
    {"id": "Kanal23.tr@SD", "name": "Kanal 23", "category": "Diğer"},
    {"id": "KanalV.tr@SD", "name": "Kanal V", "category": "Diğer"},
    {"id": "Kanal26.tr@SD", "name": "Kanal 26", "category": "Diğer"},
    {"id": "Kanal33.tr@SD", "name": "Kanal 33", "category": "Diğer"},
    {"id": "4UTV.tr@SD", "name": "4U TV", "category": "Diğer"},
    {"id": "AfroturkTV.tr@SD", "name": "Afroturk TV", "category": "Diğer"},
    {"id": "AksuTV.tr@SD", "name": "Aksu TV", "category": "Diğer"},
    {"id": "AlanyaPostaTV.tr@SD", "name": "Alanya Posta TV", "category": "Diğer"},
    {"id": "AltasTV.tr@SD", "name": "Altas TV", "category": "Diğer"},
    {"id": "AnadoluNetTV.tr@SD", "name": "Anadolu Net TV", "category": "Diğer"},
    {"id": "ATVAlanya.tr@SD", "name": "ATV Alanya", "category": "Diğer"},
    {"id": "BirTV.tr@SD", "name": "Bir TV", "category": "Diğer"},
    {"id": "BricveSatrancTV.tr@SD", "name": "Bric ve Satranc TV", "category": "Diğer"},
    {"id": "BRTV.tr@SD", "name": "BRTV", "category": "Diğer"},
    {"id": "CayTV.tr@SD", "name": "Cay TV", "category": "Diğer"},
    {"id": "Cine1.tr@SD", "name": "Cine 1", "category": "Diğer"},
    {"id": "DenizPostasiTV.tr@SD", "name": "Deniz Postasi TV", "category": "Diğer"},
    {"id": "EdessaTV.tr@SD", "name": "Edessa TV", "category": "Diğer"},
    {"id": "ElsharqTV.tr@SD", "name": "Elsharq TV", "category": "Diğer"},
    {"id": "ERTV.tr@SD", "name": "ERTV", "category": "Diğer"},
    {"id": "ErzurumWebTV.tr@SD", "name": "Erzurum Web TV", "category": "Diğer"},
    {"id": "ESTV.tr@SD", "name": "ES TV", "category": "Diğer"},
    {"id": "ETVKayseri.tr@SD", "name": "ETV Kayseri", "category": "Diğer"},
    {"id": "ETVManisa.tr@SD", "name": "ETV Manisa", "category": "Diğer"},
    {"id": "EuroD.tr@SD", "name": "Euro D", "category": "Diğer"},
    {"id": "FortunaTV.tr@SD", "name": "Fortuna TV", "category": "Diğer"},
    {"id": "HunatTV.tr@SD", "name": "Hunat TV", "category": "Diğer"},
    {"id": "IcelTV.tr@SD", "name": "Icel TV", "category": "Diğer"},
    {"id": "IlkeTV.tr@HD", "name": "Ilke TV", "category": "Diğer"},
    {"id": "Kanal12.tr@SD", "name": "Kanal 12", "category": "Diğer"},
    {"id": "Kanal15.tr@SD", "name": "Kanal 15", "category": "Diğer"},
    {"id": "Kanal3.tr@SD", "name": "Kanal 3", "category": "Diğer"},
    {"id": "Kanal32.tr@SD", "name": "Kanal 32", "category": "Diğer"},
    {"id": "Kanal58.tr@SD", "name": "Kanal 58", "category": "Diğer"},
    {"id": "KanalFirat.tr@SD", "name": "Kanal Firat", "category": "Diğer"},
    {"id": "KanalHayat.tr@SD", "name": "Kanal Hayat", "category": "Diğer"},
    {"id": "KayTV.tr@SD", "name": "Kay TV", "category": "Diğer"},
    {"id": "KentTurk.tr@SD", "name": "Kent Turk", "category": "Diğer"},
    {"id": "KocaeliTV.tr@SD", "name": "Kocaeli TV", "category": "Diğer"},
    {"id": "KonyaOlayTV.tr@SD", "name": "Konya Olay TV", "category": "Diğer"},
    {"id": "LineTV.tr@SD", "name": "Line TV", "category": "Diğer"},
    {"id": "LuysTV.tr@SD", "name": "Luys TV", "category": "Diğer"},
    {"id": "MaviKaradenizTV.tr@SD", "name": "MaviKaradeniz TV", "category": "Diğer"},
    {"id": "MercanTV.tr@SD", "name": "Mercan TV", "category": "Diğer"},
    {"id": "MTurkTV.tr@SD", "name": "MTurk TV", "category": "Diğer"},
    {"id": "NaturalTV.tr@SD", "name": "Natural TV", "category": "Diğer"},
    {"id": "OncuTV.tr@SD", "name": "Öncü TV", "category": "Diğer"},
    {"id": "SunRTV.tr@SD", "name": "Sun RTV", "category": "Diğer"},
    {"id": "TBMMTV.tr@SD", "name": "TBMM TV", "category": "Diğer"},
    {"id": "TempoTV.tr@SD", "name": "Tempo TV", "category": "Diğer"},
    {"id": "TonTV.tr@SD", "name": "Ton TV", "category": "Diğer"},
    {"id": "TV1.tr@SD", "name": "TV 1", "category": "Diğer"},
    {"id": "TV264.tr@SD", "name": "TV 264", "category": "Diğer"},
    {"id": "TV41.tr@SD", "name": "TV 41", "category": "Diğer"},
    {"id": "TV52.tr@SD", "name": "TV 52", "category": "Diğer"},
    {"id": "TVDen.tr@SD", "name": "TV Den", "category": "Diğer"},
    {"id": "UrfaNatikTV.tr@SD", "name": "Urfa Natik TV", "category": "Diğer"},
    {"id": "Van65TV.tr@SD", "name": "Van 65 TV", "category": "Diğer"},
    {"id": "ZarokTV.tr@SD", "name": "Zarok TV", "category": "Diğer"},
    {"id": "Tele1.tr", "name": "TELE1", "category": "Haber"},
    {"id": "KRT.tr", "name": "KRT TV", "category": "Haber"},
    {"id": "UlusalKanal.tr", "name": "Ulusal Kanal", "category": "Haber"},
    {"id": "TV5.tr", "name": "TV5", "category": "Haber"},
    {"id": "GZT.tr", "name": "GZT", "category": "Haber"},
]


def _fold(text: str) -> str:
    value = str(text or "").replace("ı", "i").replace("İ", "I")
    value = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in value if not unicodedata.combining(ch)).lower()


def _key(text: str) -> str:
    value = _fold(text)
    value = value.split("@", 1)[0]
    value = re.sub(r"\.(?:tr|cy|uk|de|fr|az|iq|ir|ca|us|kg)$", "", value)
    value = re.sub(r"\[[^\]]*\]", " ", value)
    value = re.sub(
        r"\([^)]*(?:\d{3,4}p|turkiye|turkey|hd|sd|uhd|4k|8k|geo-blocked|not 24/7)[^)]*\)",
        " ",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"\b(?:2160p|1440p|1080p|900p|720p|576p|540p|480p|360p|288p|hd|sd|uhd|fhd)\b",
        " ",
        value,
        flags=re.I,
    )
    return re.sub(r"[^a-z0-9]+", "", value)


_ID_INDEX: dict[str, str] = {}
_NAME_INDEX: dict[str, str] = {}

for row in CHANNELS:
    category = row["category"]
    channel_id = _key(row.get("id", ""))
    name = _key(row.get("name", ""))
    if channel_id:
        _ID_INDEX[channel_id] = category
    if name:
        _NAME_INDEX[name] = category


# Identity aliases are category-neutral; they only help match spelling/branding
# variations to a curated catalog entry.
ALIASES = {
    "a2": "a2tv",
    "now": "nowtv",
    "tv8bucuk": "tv85",
    "tv85hd": "tv85",
    "cnnturkhd": "cnnturk",
    "sozcutvtr": "sozcutv",
    "haberturk": "haberturktv",
    "benguturk": "benguturktv",
    "powerturk": "powerturktv",
    "tr24tv": "24tv",
}


def _resolve_alias(key: str) -> str:
    return ALIASES.get(key, key)


def category_for_channel(tvg_id: str = "", name: str = "") -> str | None:
    id_key = _resolve_alias(_key(tvg_id))
    if id_key and id_key in _ID_INDEX:
        return _ID_INDEX[id_key]

    name_key = _resolve_alias(_key(name))
    if name_key and name_key in _NAME_INDEX:
        return _NAME_INDEX[name_key]

    # Cross-match because some providers put a canonical service identity in
    # the display label but omit tvg-id, or vice versa.
    if id_key and id_key in _NAME_INDEX:
        return _NAME_INDEX[id_key]
    if name_key and name_key in _ID_INDEX:
        return _ID_INDEX[name_key]

    return None
