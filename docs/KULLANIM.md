# Kullanım kılavuzu (tasarımcılar için)

Bu belge teknik bilgi gerektirmez. 15 dakikanızı alır.

---

## 0. Önce şunu yapın: senkronu bitirin

Creative Cloud masaüstü uygulamasını açın → **Files** sekmesi → senkron
tamamlanana kadar bekleyin. Araç yerel klasörü de kopyaladığı için, senkronu
tamamlanmış bir makinede yedek çok daha hızlı ve eksiksiz olur.

> Araç, 0 baytlık dosya bulursa sizi uyarır — bu, o dosyanın henüz
> inmediği anlamına gelir.

---

## 1. Uygulamayı açın

**Windows:** `Adobe CC Rescue.exe` → SmartScreen çıkarsa *Ek bilgi* → *Yine de çalıştır*.
**macOS:** `.dmg`'yi açın, uygulamayı Applications'a sürükleyin, **sağ tık → Aç** → yine *Aç*.

---

## 2. Yalnızca yerel yedek istiyorsanız

Hiçbir şey yapmanıza gerek yok:

1. **Hedef klasör** seçin (varsayılan: Masaüstü)
2. **Yedeklemeyi başlat**

Bu, yerel Creative Cloud Files klasörünüzü, fırça/aksiyon/renk ön ayarlarınızı,
Lightroom Classic kataloğunuzu ve aktif font listenizi kurtarır.

---

## 3. Bulut dosyaları da istiyorsanız: giriş yapın

**Adobe ile giriş yap** düğmesine basın.

Adobe'nin kendi giriş sayfası açılır — her zamanki gibi e-posta ve şifrenizle
(veya şirket SSO'nuzla) oturum açın. Giriş tamamlanınca pencere kendiliğinden
kapanır ve yeşil bir onay görürsünüz:

```
✓ Giriş yapıldı: kullanici@sirket.com · 4s 12dk geçerli
```

Hepsi bu. Kopyalanacak, yapıştırılacak bir şey yok.

> **Şifreniz nereye gidiyor?** Yalnızca Adobe'ye. Açılan pencere Adobe'nin
> gerçek giriş sayfasıdır; uygulama şifrenizi görmez, kaydetmez. Sadece giriş
> sonrası Adobe'nin sayfaya bıraktığı oturum anahtarını okur ve yalnızca
> kendi bilgisayarınızda saklar.

> Giriş yaptıktan sonra pencere birkaç saniye içinde kapanmazsa, pencereyi
> kendiniz kapatın — uygulama kapanış anında oturumu yine de alır.

Oturum birkaç saat geçerlidir. Süresi dolarsa araç durur ve size söyler;
**Adobe ile giriş yap**'a tekrar basıp kaldığınız yerden devam edersiniz —
inen dosyalar korunur.

### Giriş penceresi açılmazsa

Windows'ta "giriş penceresi bileşeni yok" uyarısı alırsanız **Microsoft Edge
WebView2 Runtime** kurun (çoğu Windows 10/11'de zaten var):
<https://developer.microsoft.com/microsoft-edge/webview2/>

Yine olmazsa **Gelişmiş** bölümünden jeton veya HAR ile giriş yapabilirsiniz —
oradaki "Nasıl alınır?" düğmesi adımları anlatır.

> ⚠️ Jetonu elle kopyalarken `authorization` satırına **sağ tıklayıp "Copy value"**
> deyin. Fareyle seçip kopyalarsanız Chrome uzun başlığı `…` ile kısaltır, jeton
> eksik gelir ve hiçbir bulut isteği çalışmaz. Uygulama bunu fark edip uyarır.

---

## 4. Başlatın

**Yedeklemeyi başlat**. Pencereyi kapatmayın. Bitince:

* `rapor.html` otomatik açılır — ne indi, ne kaldı
* `YAPILACAKLAR.md` — elle yapılacaklar

Kesildi mi? Uygulamayı tekrar açıp aynı hedef klasörle başlatın. İnmiş dosyalar
atlanır.

---

## 4b. Bulut belgelerini katmanlı PSD'ye çevirme (EN ÖNEMLİ ADIM)

İndirilen `.psdc` dosyaları **Photoshop'ta açılamaz** — Adobe izin vermiyor.
Katmanlı, düzenlenebilir kopya için belgelerin Photoshop'tan geçmesi gerekiyor.
Bunu **hesap geçişinden önce** yapın; sonrasında belgeler kaybolur.

Araç bunu sizin yerinize yapar. Tıklamanız gereken bir şey yok.

### Gerekenler

* **Photoshop 28 veya üstü**, iş hesabınızla açık (belgelerin sahibi olan hesap)
* Daha önce alınmış bir yedek (`manifest.json` dosyası için)
* Kaynaktan çalıştırıyorsanız `pip install pywin32` (hazır `.exe` içinde var)

### Çalıştırma

Photoshop açıkken, ayrı bir komut penceresinde:

**Uygulamadan:** yedekleme bittikten sonra **Bulut belgelerini PSD'ye çevir**
düğmesine basın. Hepsi bu.

**Komut satırından:**

```
adobe-rescue.exe psd --out "%USERPROFILE%\Desktop\PSD"
```

Belge listesini kendisi bulur (Masaüstündeki `Adobe-Backup-*` klasörlerinden
en yenisi). Başka bir yedeği kullanmak isterseniz `--names <manifest.json>`
ekleyin.

> **Yönetici olarak çalıştırmayın.** Photoshop normal kullanıcı olarak açıksa
> yönetici penceresinden bağlanılamaz. Normal bir komut penceresi kullanın.

Kaynaktan çalıştırıyorsanız: `python -m adobe_rescue psd --out ... --names ...`

Her belge tek tek açılır, `.psd` olarak kaydedilir ve kapatılır:

```
[1/61] Balcony_Bg.psdc … ✓ 18,137,976 bayt · 2 katman · 15 sn
[2/61] BubbleSpawner.psdc … ✓ 34,221,104 bayt · 9 katman · 22 sn
```

61 belge yaklaşık **15-25 dakika** sürer. İlk birkaçını izleyin, sonrasında
bırakabilirsiniz.

> **Renk ayarı önerisi:** çevirmeden önce Photoshop'ta
> **Düzen > Renk Ayarları**'ndan RGB çalışma alanını **sRGB IEC61966-2.1**
> yapın. Çalışma alanınız monitöre özel bir profilse (örn. "Monitor RGB"),
> dosyalar o makineye özgü bir profille etiketlenebilir ve renkler başka
> bilgisayarlarda kayabilir. Piksel verisi her durumda korunur.

### Notlar

* **Önce deneyin:** komut satırında sonuna `--limit 2` ekleyin, iki belgeyi çevirsin. Çıkan
  PSD'yi açıp katmanların yerinde olduğunu görün.
* **Durdurabilirsiniz:** `Ctrl+C`. Tekrar çalıştırdığınızda kaldığı yerden
  devam eder, çevrilmiş dosyayı tekrar yazmaz.
* **Bulut belgeleriniz değiştirilmez:** kopya olarak kaydedilir, kaydedilmeden
  kapatılır.
* **Photoshop'a dokunmayın** çalışırken. Zaten açık olan belgeleriniz
  kapatılmaz; kaydedilmemiş çalışmanız varsa o belge atlanır.
* 2 GB'ı aşan belgeler otomatik `.psb` olarak kaydedilir.
* **Renk profili uyarıları çıkmaz.** Photoshop "Embedded Profile Mismatch" gibi
  pencerelerle işlemi durdurabiliyor; araç bu pencereleri kapatıp Photoshop'un
  kendi renk politikasını uygulatıyor (standart ayarlarda **gömülü profil
  korunur** — sRGB sRGB kalır). Her belgenin profili ekranda yazar, böylece ne
  kaydedildiğini görürsünüz. Renk ayarlarınız değiştirilmez.
* Eksik font uyarıları da sessizce geçilir; metin katmanları olduğu gibi
  kaydedilir, yeni hesapta fontları etkinleştirince yerine oturur.
* **"Shared with you"** belgeleri sizin değil; yedekte de yoklar.

### Photoshop 28'den eskiyse

Betikle bulut belgesi açılamıyor. Araç bunu görüp izleme moduna geçer:

```
adobe-rescue.exe psd --out ... --names ... --watch
```

Sonra Photoshop'ta **Dosya > Bulut Belgesi Aç** deyin, bir dosyaya tıklayıp
**Ctrl+A** ile hepsini seçin ve **Aç** deyin. Açılan belgeleri araç kaydedip
kapatır. Photoshop yavaşlarsa 15'er seçin.

---

## 5. Elle yapılacak iki şey

Adobe bu ikisini otomatikleştirmeye izin vermiyor.

### Kütüphaneler (`.cclibs`)

1. Creative Cloud **masaüstü uygulaması** → **Files** → **Your libraries**
2. Kütüphanenin üzerine gelin → **Export**
3. Yedek klasöründeki `libraries/` içine kaydedin

Renk, font ve şablonlar tek tek dışa aktarılamaz — **kütüphaneyi bütün olarak** alın.
Hangi kütüphanelerin olduğu `libraries/kutuphane-envanteri.json` dosyasında yazar.

### Adobe Express

`express/express-projeleri.html` dosyasını açın — tüm projeleriniz tıklanabilir
listede. Her birinde: **Share → Download** → baskı işleri için PDF, dijital için
PNG/MP4.

---

## 6. Bitirin

1. **Klasörü aç** → içeriği gözden geçirin
2. Tek dosya isterseniz: `adobe-rescue package` (veya klasörü sağ tık → sıkıştır)
3. Yedeği **harici bir diske veya ekip paylaşımına** kopyalayın —
   makinenizin kendisi bir yedek değildir

---

## Yeni hesap açıldıktan sonra

- [ ] `fonts/aktif-fontlar.csv` listesindeki fontları fonts.adobe.com'dan tekrar etkinleştirin
- [ ] `.cclibs` dosyalarını içe aktarın (masaüstü uygulaması → Files → Your libraries → Import)
- [ ] Ön ayarları ilgili uygulamalara yükleyin (`presets/` klasörü)
- [ ] Bulut belgelerini (PSD/AI) yeni hesabın bulutuna geri yükleyin

---

## Sorun giderme

| Belirti | Çözüm |
|---|---|
| "Oturum yok" | Yalnızca yerel kaynaklar inecek. Jeton eklemek için 3. adım. |
| "Jetonun süresi dolmuş" | HAR'ı yeniden alın. İndirilenler korunur. |
| "Uç nokta bulunamadı (404)" | Adobe URL değiştirmiş. `login --har` ile yeni HAR verin. |
| Hata veren dosyalar | `adobe-rescue pull --retry-failed` |
| Disk uyarısı | Hedef klasörü daha büyük bir diske alın. |
| 0 baytlık dosyalar | CC masaüstü senkronu bitmemiş. Bekleyip tekrar çalıştırın. |
| Photoshop'ta bulut belgeleri paneli açılmıyor / "asset organizer" hatası | Bilgisayarda Adobe adreslerini engelleyen bir ayar olabilir: `C:\Windows\System32\drivers\etc\hosts` dosyasında `adobe.io` / `adobe.com` satırları ve güvenlik duvarında Adobe kuralları var mı bakın. Bazı "optimizasyon" araçları bunları ekler ve bulut belgeleri çalışmaz. |
