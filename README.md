# Adobe CC Rescue

Adobe hesapları yeni bir ana hesaba taşınmadan önce, bir kullanıcının Creative
Cloud'daki her şeyini **tek seferde** yerel diske indiren toplu yedekleme aracı.
Windows ve macOS için çift tıklanabilir uygulama olarak dağıtılır.

> **Neden:** Hesaplar yeni ana hesap altında yeniden açıldığında bulut dosyaları,
> kütüphaneler, ön ayarlar ve aktif fontlar **transfer olmaz** — sıfır hesap gibi
> başlar. Son tarih: **Cumartesi 23:00**.

---

## Ne yedekler

| Kaynak | Ne yapar | Oturum |
|---|---|---|
| `local-sync` | Yerel **Creative Cloud Files** klasörünün tamamını kopyalar (web'den tek tek indirmekten hızlı ve güvenilir) | — |
| `presets` | Fırça (`.abr`), aksiyon (`.atn`), stil (`.asl`), renk örneği (`.aco/.ase`), gradyan, script, workspace — Ps/Ai/Id/Pr/Ae | — |
| `fonts` | Aktif **Adobe Fonts** listesini CoreSync veritabanından çıkarır → `aktif-fontlar.txt` + CSV | — |
| `local-extras` | Lightroom **Classic** kataloglarını (`.lrcat`) bulur ve kopyalar | — |
| `cloud-files` | Bulut dosya ağacını baştan sona gezer, paralel ve devam ettirilebilir şekilde indirir | ✓ |
| `cloud-docs` | Bulut belgelerini (`.psdc`, `.aic`) düz **PSD/AI**'ya dönüştürüp indirir; dönüşmeyenleri el listesine yazar | ✓ |
| `acrobat` | Document Cloud'daki tüm PDF'leri indirir | ✓ |
| `lightroom` | Lightroom (bulut) orijinal master dosyalarını indirir | ✓ |
| `libraries` | Kütüphaneleri envanterler ve her biri için `.cclibs` dışa aktarma adımını listeye koyar | ✓ |
| `express` | Express projelerini listeler, tıklanabilir bir HTML indeks üretir | ✓ |

### Bulut belgeleri → katmanlı PSD

İndirilen `.psdc` dosyaları Photoshop'ta açılamaz (Adobe izin vermiyor).
`tools/ps_batch_psd.py` belgeleri tek tek Photoshop'ta açıp katmanlı `.psd`
olarak kaydeder — tamamen otomatik, 61 belge ~20 dakika. Ayrıntı:
[KULLANIM.md](docs/KULLANIM.md#4b-bulut-belgelerini-katmanlı-psdye-çevirme-en-önemli-adım)

Her çalıştırmada üç şey üretilir:

* `rapor.html` — ne indi, ne inmedi, ne kaldı (açık/koyu tema)
* `YAPILACAKLAR.md` — elle yapılacakların işaretlenebilir listesi
* `manifest.json` / `manifest.csv` — her dosyanın sha256'sı, boyutu, durumu

## Bells & whistles

* **Devam ettirilebilir** — yarıda kesilen indirme HTTP Range ile kaldığı yerden sürer; `.part` dosyası tamamlanınca adıyla yerine geçer
* **Paralel** — ayarlanabilir eşzamanlılık, global hız sınırlayıcı, `Retry-After` uyumlu üstel geri çekilme
* **Doğrulanabilir** — her dosya sha256 ile mühürlenir, `verify` komutu yedeği baştan sona denetler
* **Yeniden çalıştırılabilir** — tamamlanmış dosyalar atlanır, yalnızca eksikler indirilir
* **Filtreler** — `--include`, `--exclude`, `--max-size-mb`, `--only`, `--skip`, `--dry-run`
* **Tek tıkla giriş** — Adobe'nin kendi giriş sayfası uygulama içinde açılır (SSO ve 2FA dahil); jeton kullanıcıya hiç gösterilmez
* **Uç nokta kurtarma** — gerekirse uç noktalar bir DevTools **HAR** dosyasından çıkarılır, Adobe URL'lerini değiştirse bile çalışır
* **Türkçe arayüz** — Türkçe karakterli dosya adları Windows'ta da bozulmaz (NFC + rezerve ad koruması)
* **Son tarih sayacı** — arayüzde ve raporda Cumartesi 23:00'e kalan süre

---

## Kurulum

### Son kullanıcı (tavsiye edilen)

[Releases](../../releases) sayfasından indirin:

| Dosya | Kime |
|---|---|
| `adobe-cc-rescue-windows.zip` | Windows → içindeki **Adobe CC Rescue.exe** |
| `adobe-cc-rescue-macos-arm64.dmg` | Mac (Apple Silicon, M1–M4) |

Uygulamalar imzalı değildir:
**macOS** ilk açılışta sağ tık → *Aç* → yine *Aç*.
**Windows** SmartScreen uyarısında *Ek bilgi* → *Yine de çalıştır*.

### Kaynaktan

```bash
git clone <repo-url>
cd adobe-cc-rescue
pip install -r requirements.txt
python -m adobe_rescue doctor        # ortam kontrolü
python -m adobe_rescue gui           # pencereli arayüz
```

---

## Kullanım

```bash
# 0) Ortamı kontrol et
adobe-rescue doctor

# 1) Giriş (yalnızca bulut kaynakları için gerekir)
adobe-rescue login                               # Adobe giriş penceresi açılır
adobe-rescue login --har ~/Downloads/adobe.har   # [gelişmiş] pencere açılmazsa
adobe-rescue login --token "eyJhbGciOi..."       # [gelişmiş]

# 2) Her şeyi indir
adobe-rescue pull -o ~/Desktop/Adobe-Yedek

# Oturum açamıyorsanız — yerel her şey yine de kurtarılır
adobe-rescue pull --local-only

# Seçerek
adobe-rescue pull --only cloud-files,acrobat --exclude "*.tmp" --max-size-mb 500
adobe-rescue pull --dry-run              # sadece listele, indirme

# 3) Doğrula, paketle
adobe-rescue verify
adobe-rescue package --archive ~/Desktop/adobe-yedek.zip
```

Kesilirse aynı komutu tekrar çalıştırın — kaldığı yerden devam eder.

Jeton nasıl alınır (ayrıntılı): [docs/KULLANIM.md](docs/KULLANIM.md)

---

## Bilmeniz gerekenler

**Adobe'nin kişisel bulut depolaması için açık bir toplu indirme API'si yok.**
Bu araç, Adobe'nin kendi giriş sayfasını uygulama içinde açar (Windows'ta
WebView2, macOS'ta WKWebView). Şifre uygulamaya hiç uğramaz; giriş sonrası
Adobe'nin sayfaya bıraktığı oturum anahtarı okunur ve Creative Cloud web
uygulamalarının çağırdığı **belgelenmemiş** uç noktalara gidilir. Sonuçları:

* Oturum genelde **birkaç saat** geçerlidir. Süre dolarsa araç durur ve size söyler;
  tekrar giriş yapıp devam edersiniz — indirilenler korunur.
* Adobe uç noktaları habersiz değiştirebilir. `login --har` bu yüzden var: uç
  noktaları sizin tarayıcı trafiğinizden öğrenir, sabit kodlanmış URL'lere bağımlı değildir.
* Kendi hesabınızın verisini indirirsiniz; aracın başka bir hesaba erişimi yoktur.
* Oturum anahtarı yalnızca kullanıcı profilinizdeki `session.json` dosyasında
  tutulur (yalnız size okunur izinle), yedek klasörüne yazılmaz, günlüklere düşmez
  ve hiçbir yere gönderilmez.

**Otomatikleştirilemeyen iki şey var** (Adobe izin vermiyor), araç bunları
listeler ve adım adım tarif eder:

1. **Kütüphaneler** → yalnızca masaüstü uygulamasından `.cclibs` olarak dışa aktarılır
2. **Adobe Express** → her proje Express içinde PDF/PNG/MP4'e dönüştürülüp indirilir

---

## Geliştirme

```bash
python -m unittest discover -s . -p "test_*.py" -v
```

23 test; mock bir Adobe API sunucusuna karşı ağaç gezme, paralel indirme, Range
ile devam, 5xx tekrar denemesi, jeton süre dolumu ve manifest üzerinden devam
senaryolarını kapsar.

Yeni kaynak eklemek: `adobe_rescue/sources/` altında `Source` sınıfından türetip
`REGISTRY`'ye ekleyin — motor, manifest, rapor ve arayüz kendiliğinden devralır.

## Lisans

MIT
