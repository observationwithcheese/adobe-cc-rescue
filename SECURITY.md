# Güvenlik

## Kimlik bilgisi kapsamı

Bu araç Adobe oturum jetonunu **yalnızca Adobe adreslerine** gönderir.
İndirme bağlantıları çoğu zaman üçüncü taraf CDN/S3 adreslerine yönlendirir
ve bu adresler yetkilendirmeyi kendi sorgu dizelerinde taşır; oraya hiçbir
kimlik bilgisi eklenmez. Alan adı eşleşmesi etiket sınırında yapılır
(`notadobe.com` Adobe sayılmaz). Testlerle sabitlenmiştir
(`TestCredentialScope`).

## Oturum nerede durur

Giriş, Adobe'nin kendi sayfasında yapılır; şifre bu uygulamaya hiç uğramaz.
Oturum anahtarı yalnızca kullanıcı profilindeki `session.json` dosyasında,
yalnız sahibine okunur izinle tutulur. Yedek klasörüne yazılmaz, günlüklere
düşmez, hiçbir yere gönderilmez. Depoda hiçbir zaman jeton, çerez veya hesap
kimliği bulunmaz.

## Photoshop'a dokunurken

PSD'ye çevirme adımı belgeleri **kopya olarak** kaydeder ve **kaydetmeden**
kapatır; bulut belgeniz değişmez. Yalnızca aracın kendi açtığı ve
kaydedilmemiş değişikliği olmayan bulut belgeleri kapatılır.

## Sorun bildirimi

Güvenlikle ilgili bir sorun görürseniz issue açmadan önce depo sahibine
yazın.
