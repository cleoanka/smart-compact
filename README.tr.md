# smart-compact

[Claude Code](https://code.claude.com/docs) compact yaptığında neyin sağ çıkacağına sen karar ver: asla kaybolmaması gerekeni pinle, kesin çöp olanı işaretle, gerisi güncel hedefe göre değerlendirilsin. Tamamı hook'larda çalışır; işaretlemek ek bir model çağrısı gerektirmez.

[English](README.md)

> Resmî olmayan bir topluluk aracıdır. Anthropic ile bağlantısı yoktur, Anthropic tarafından onaylanmamıştır.

## Sorun

Uzun bir oturum compact olduğunda Claude Code geçmişi, modelin yazdığı bir özetle değiştirir. Özetin neyi tutacağını sen belirleyemezsin. İki saat önce söylediğin bir kısıt başka kelimelerle dönebilir ya da hiç dönmeyebilir; sildiğin bir dosyanın içeriği ise yer kaplamaya devam edebilir.

"Oluşturulan dosyalar hep kalsın" gibi sabit kurallar işe yaramaz, çünkü önem duruma bağlıdır: tek kullanımlık bir betik gürültüdür, teslim etmek üzere olduğun betik değildir.

## Nasıl çalışır

Model bir satırı yazdığı anda işaretler; o satırın önemli olup olmadığını en iyi o an bilir. Compact başladığında bir hook, işaretleri oturum kaydından okur.

| İşaret | Anlamı | Compact sırasında ne olur |
| :- | :- | :- |
| `◆ metin` | Pin | Hook kaydeder ve her compact'tan sonra (normal ya da akıllı) **aynen** geri koyar |
| `◇ metin` | Silme | Satır ve adını andığı şey, özeti yazan modele "tamamen dışarıda bırak, işaretçi de koyma" diye bildirilir |
| `◆✗ parça` | Pin kaldırma | İçinde o parça geçen eski pini düşürür |
| *(işaretsiz)* | Kararsız | Compact anında güncel hedefe göre değerlendirilir |

İşaret, bir yanıtın ya da senin kendi mesajının sembolle başlayan satırıdır. Araç çağrıları, araç çıktıları, `!` kabuk komutlarının çıktısı, alt ajan metinleri, düşünme metni, alıntı satırları (`> …`) ve kod blokları taranmaz.

```
yanıt metni ─► oturum kaydı (diskte .jsonl, asla yeniden yazılmaz)
                     │
/compact ───────────►│ PreCompact hook'u işaretleri okur
                     ├─ ◆ pinler  ─► kaydedilir ─► SessionStart hook'u aynen geri yükler
                     ├─ ◇ silme   ─► özet talimatına silme listesi eklenir
                     └─ akıllı mı? ─► özet talimatına KEEP / POINTER / DROP politikası eklenir
```

İşaretsiz içerik için akıllı compact, özeti yazan modele her öğe için tek soru sordurur: *bu atılırsa işe devam ederken hata ya da tekrar iş çıkar mı?* Yanıta göre öğe üç kademeden birine girer:

- **KEEP**, ayrıntılı: gerekçeli kararlar, doğrulanmış bilgiler, kullanıcı düzeltmeleri, güncel durum, hedefin hâlâ ihtiyaç duyduğu kimlikler ve yollar.
- **POINTER**, tek satır: işe yarayabilecek şeyler `ne -> nerede` biçimine iner (bir yol ya da "transcript"). Model emin değilse bu kademeyi seçmek zorundadır.
- **DROP**: ham araç çıktısı, dosya içerikleri, yerini yenisine bırakmış denemeler.

Hiçbir şey gerçekten kaybolmaz. Compact, kayıt dosyasının sonuna ekleme yapar, dosyayı yeniden yazmaz; tam geçmiş diskte durur.

### Kayıttan geri okumak, bedelini ödemeden

Kayıt dosyası okunacak bir şey değildir. Her satır bütün bir mesajdır; araç çıktısı taşıyan bir satır on binlerce karakter olabilir. Model bir ayrıntı için dosyayı grep'lerse satırın tamamı context'e girer ve sonraki her turda yeniden okunur.

Bu yüzden hook, her compact'tan sonra modele çıplak dosya yolu yerine bir `recall` komutu verir:

```
smart-compact.py recall <kayıt> retry_budget degraded
[line 212 2026-11-15T10:08 tool_result] …status=ok latency_ms=412 2026-11-15T10:08:37 worker=12 shard=k7 status=degraded retry_budget=17 owner=team-osprey 2026-11-15T10:08:38 worker=3…
```

- Bütün kelimeler aynı mesajda geçmelidir. En yeni eşleşmeler önce gelir.
- Her eşleşme, eşleşen yerin etrafında yaklaşık 480 karakterlik bir pencereye kesilir. Eşleşen satırlar ne kadar büyük olursa olsun tek çağrı en çok 6 eşleşme ve 4.000 karakter basar.
- Parça yetmezse `--line N [--at OFFSET]` tek bir mesajı 3.000 karakterlik sayfalarla gezer.
- `--discards`, son compact'ta dışarıda bırakılması istenenleri listeler; bir silme işareti yanlış çıkarsa buradan bulunur.

Yanıt metinlerinde, araç girdilerinde, araç çıktılarında ve kabuk çıktısında arar. Dosya üzerinde düz bir metin aramasıdır: dizin yok, model çağrısı yok.

## Kurulum

```bash
git clone https://github.com/cleoanka/smart-compact
cd smart-compact
python3 install.py --claude-md
```

Sonra yeni bir Claude Code oturumu aç.

Kurulum betiği:

- `hooks/smart-compact.py` dosyasını `~/.claude/hooks/` altına kopyalar
- `~/.claude/settings.json` dosyasına beş hook girdisi ekler; önce dosyanın yanına yedeğini alır, diğer hook ve ayarlarına dokunmaz
- `~/.claude/commands/` altına yedi küçük komut dosyası yazar
- `--claude-md` verilirse [`docs/claude-md-rule.md`](docs/claude-md-rule.md) içindeki işaretleme kuralını `~/.claude/CLAUDE.md` sonuna ekler

Kural önemlidir: o olmadan model işaretlerin varlığını bilmez; yalnız komutlar ve senin koyduğun işaretler çalışır.

| Seçenek | Etkisi |
| :- | :- |
| `--lang en` ya da `--lang tr` | Komut çıktılarının dili. Varsayılan: sistem dilinden alınır |
| `--python YOL` | Hook'ların çalışacağı yorumlayıcı. Varsayılan: kurulumu çalıştıran yorumlayıcı; o bir virtualenv ise temel yorumlayıcısı |
| `--config-dir YOL` | Başka bir Claude Code ayar dizini kullan. Varsayılan: `$CLAUDE_CONFIG_DIR` ya da `~/.claude` |
| `--uninstall` | Hook'ları, komutları ve betiği kaldırır. Kayıtlı durum `~/.claude/smart-compact/` altında kalır |

Betiği yeniden çalıştırmak mevcut kurulumu günceller, hiçbir şeyi çoğaltmaz. Bu adlardan biriyle kendi komutun zaten varsa (örneğin `/drop`), kurulum ona dokunmaz ve smart-compact o komutu yakalamaz.

**Gereksinimler:** Python 3.9 ya da üstü ve `PreCompact`, `PostCompact`, `SessionStart`, `UserPromptExpansion`, `Stop` hook'larını destekleyen bir Claude Code sürümü. macOS üzerinde Claude Code 2.1.325 ile geliştirildi ve denendi.

## Kullanım

### İşaretler

Kural `CLAUDE.md` içindeyken model işaretleri kendiliğinden yazar:

```
report.py artık çalışıyor ve out/ozet.csv dosyasını yazıyor.

◆ Kısıt: yalnız standart kütüphane, pandas yok.
◆ Kök neden: csv.DictReader amount'u str döndürüyordu; int ile toplanınca TypeError veriyordu.
◆ Teslim: src/report.py, çıktı out/ozet.csv
```

```
yuzde.py silindi.

◆✗ Teslim: src/yuzde.py
◆ Karar: yuzde.py kullanıcı isteğiyle silindi; yüzde fikri kapandı.
◇ yuzde.py'nin içeriği, tasarım notları ve örnek çıktısı
```

Kendin de işaretleyebilirsin. Mesajının bir satırını `◆ `, `◇ `, `!pin ` ya da `!drop ` ile başlat ya da aşağıdaki komutları kullan.

### Komutlar

Hepsini hook yanıtlar. Modele ulaşmazlar, token harcamazlar.

| Komut | Ne yapar |
| :- | :- |
| `/pin <metin>` | Tek satır pinler |
| `/unpin <numara ya da parça>` | Bir pini kaldırır |
| `/drop <metin>` | Bir şeyi sıradaki compact için işe yaramaz diye işaretler |
| `/undrop <numara ya da parça>` | Kullanılmadan önce bir silme işaretini kaldırır |
| `/pins` | Pinleri ve bekleyen silme işaretlerini listeler |
| `/smartcompact [odak]` | Akıllı politikayla şimdi compact yapar |
| `/smartautocompact [on\|off\|300k\|40%\|durum]` | Bu oturum için akıllı otomatik compact |

### Hangi compact ne yapar

| | Pinler geri gelir | Silme listesi uygulanır | KEEP / POINTER / DROP politikası |
| :- | :-: | :-: | :-: |
| `/compact`, normal otomatik compact | evet | evet | hayır |
| `/smartcompact` ya da `/compact smart` | evet | evet | evet |
| Akıllı oto açıkken otomatik compact | evet | evet | evet |

Düz `/compact`, Claude Code'un kendi özet tarzını korur; yani başka hiçbir şeyi değiştirmeden yalnız pinleri kullanabilirsin. `/compact smart: auth işine odaklan` yazarsan politikaya kendi odağını eklersin.

### Akıllı otomatik compact

Normal otomatik compact, context neredeyse dolana kadar bekler; 1M context'li modellerde bu yaklaşık 967K token eder. `/smartautocompact 300k`, bir tur 300K'nın üstünde bittiği anda akıllı politikayla compact yapar.

- Ayar, yazdığın oturuma aittir. Diğer oturumları etkilemez; yeni oturumlar kapalı başlar.
- Sınır, normal otomatik compact penceresini aşamaz. Aşacaksa ona eşitlenir.
- Alt sınır 100K'dır; Claude Code'un kendi alt sınırıyla aynıdır.
- Compact'tan sonraki ilk turda context hâlâ sınırın üstündeyse yeniden compact yapmak işe yaramaz; akıllı oto o oturumda durur.

### `/compact` komutunu senin yerine yazması

Bir hook compact başlatamaz, özel bir komut da başlatamaz. Bu yüzden `/smartcompact` ve akıllı otomatik compact, terminaline `/compact smart` yazar:

- **tmux** içinde `tmux send-keys` ile
- macOS'ta **iTerm2** içinde AppleScript ile (macOS, iTerm2'yi denetleme izni için bir kez sorabilir)

Başka terminallerde `/smartcompact` yazman gereken komutu gösterir, akıllı oto ise kendi kendine tetikleyemez. Pinler, silme işaretleri ve elle yazılan `/compact smart` her yerde çalışır.

## Maliyet: cache, token, context

### Ne ekler

- **Model çağrısı eklemez.** İşaretler, modelin zaten yazmakta olduğu yanıtın içindeki birkaç karakterdir. Komutları hook yanıtlar.
- **Normal çalışmada cache'i bozmaz.** Sistem promptunu, araç listesini ya da önceki mesajları değiştirmez; [prompt cache'i geçersiz kılan](https://code.claude.com/docs/en/prompt-caching.md#actions-that-invalidate-the-cache) şeyler bunlardır.
- **`CLAUDE.md` içindeki kural**: yaklaşık 450 token sabit context; proje bağlamının geri kalanıyla birlikte cache'lenir.
- **Compact sırasında**: özet isteğine en çok yaklaşık 1.700 karakter politika ve 3.400 karakter silme listesi eklenir; ardından en çok 8.000 karakter pin geri yüklenir, recall komutu için de yaklaşık 500 karakter.

### Ne kazandırır

- **Özet küçükse sonraki her tur ucuzlar.** Claude Code her turda konuşmanın tamamını yeniden gönderir ve onu yeniden okumak için cache'li token ücreti öder; özetin taşıdığı her şey, sıradaki compact'a kadar her turda yeniden ödenir. Silinenler ve tek satıra inen işaretçiler bunu küçültür.
- **Bir şeye bakmak küçük kalır.** `recall`, kayıt satırının tamamını değil eşleşmenin etrafındaki pencereyi döndürür; bir arama context'e, geldiği araç çıktısının tamamını değil birkaç yüz token ekler.
- **Tekrarlanan hatalar azalır.** Kaybolan bir kısıtın bedeli token'dan çok daha fazladır: model işi yeniden yapar ya da koyduğun kuralı çiğner. Pinler, özeti yazan modelin kararından bağımsız olarak kelimesi kelimesine geri gelir.
- **Daha erken ve senin istediğin anda compact.** 900K token'a ulaşan bir oturum her turda 900K'ya kadar cache'li token okur. Daha düşük bir akıllı sınır her turu küçük tutar.
- **Cache sıcakken compact.** Özet isteği, [cache sıcakken geçmişi cache'ten okur, soğukken tam fiyattan yeniden işler](https://code.claude.com/docs/en/prompt-caching.md#compacting-the-conversation). Akıllı oto, turun sonunda, cache az önce kullanılmışken tetiklenir.

### Ne yapmaz

- Bir compact'tan *önce* context'ten hiçbir şey silemez. `◇` satırı, sıradaki compact'a kadar context'te kalır. Claude Code, bir hook'a canlı context'i düzenleme olanağı vermez.
- Compact'ın kendisi bedava değildir. Konuşma cache'ini geçersiz kılar, sonraki tur onu yeniden kurar. Fazla düşük bir sınır bu bedeli sık ödetir ve kazandırdığından fazlasını götürebilir. Akıllı otonun alt sınırı olması ve compact işe yaramadığında durması bu yüzdendir.
- Normal otomatik compact penceresi kullanıcı düzeyindeki `settings.json` dosyasından ve ortam değişkenlerinden okunur; bulunamazsa yaklaşık 967K varsayılır. 200K context'li bir modelde ya da pencere proje veya yönetilen ayarlarda tanımlıysa, akıllı sınırı gerçek pencerenin altına kendin koy.
- `/compact smart` komutunu senin yerine yazmak bir tuş gönderimidir. O anda yeni bir istem yazıyorsan metin taslağına karışır.
- `/pin` ile eklenen pinler oturum kimliğine bağlıdır. Çatallanan ya da yeni kimlikle sürdürülen bir oturum, kayıtta yazılı pinleri taşır; komutla eklenenleri taşımaz.
- Bir pini anan ya da bir pin tarafından anılan silme işareti yok sayılır: koruma, silmeye üstün gelir. Bunun ötesinde hook, bir işaretin isabetli olup olmadığını bilemez.
- Silme listesi bir dil modeline verilen talimattır. Aşağıdaki testlerin hepsinde uyuldu, ayrıca hook, işaretlenen satırlardan kaçının özete geri alıntılandığını kaydeder (`log.jsonl` içinde `drop_leaks`); ama pinler gibi kesin bir garanti değildir.

## Güvenlik notu

Pin, her compact'tan sonra modelin önüne yeniden konan metindir; yani pin güvenilir metin sayılır. Hook yalnız modelin kendi yanıt metnini ve senin yazdıklarını okur. Araç çıktıları, kabuk çıktısı, alt ajanlar, alıntı satırları ve kod blokları atlanır; bir web sayfası ya da dosya yalnızca okunmuş olmakla işaret oluşturamaz. Geriye tek yol kalır: model dışarıdan gelen bir metni, alıntılamadan, `◆` ya da `◇` ile başlayan bir satır olarak kendisi tekrarlarsa o satır sayılır. Güvenilmeyen içerikle çalışırken `/pins` ile bak, beklemediğin bir şey varsa `/unpin` ya da `/undrop` ile kaldır.

## Neler denendi

Gerçek oturumlar, Claude Code 2.1.325, macOS:

| Kontrol | Sonuç |
| :- | :- |
| Model işaretleri kendiliğinden yazıyor mu (Mythos 5.2, üç turluk iş: üç kısıtla bir betiği düzelt, ikinci bir betik yaz, sonra ondan vazgeç) | Kısıtlar, kök neden ve teslim için 5 pin; ardından vazgeçilen dosya için `◆✗`, karar için bir pin ve dosyanın içeriği için bir `◇` |
| `/smartcompact` sonrası pinler | 6 pinin hepsi aynen geri geldi |
| `/smartcompact` sonrası silme işareti | Vazgeçilen dosyanın içeriği ve çıktısı özette yoktu; aynı oturumun çatalında, hook'lar kapalıyken yapılan düz `/compact` bunları tuttu |
| O oturumda özet boyutu | Akıllı 4.544 karakter, normal 6.257 karakter |
| `/drop` ardından düz `/compact` | İşaretlenen öğe özetten çıktı; pinler geri geldi; daha önce kullanılmış eski `◇` yeniden listelenmedi |
| Komutlar | Model çağrısı yok |
| Compact sonrası recall (Opus 5.5): 800 satırlık bir log okundu, oturum compact edildi, dosya silindi, ardından sıradan bir satır soruldu | Model `recall` komutunu kendiliğinden bir kez çalıştırdı ve doğru yanıtladı. Araç çıktısı 1.001 karakterdi; dosyayı taşıyan kayıt satırı yaklaşık 40.000 karakter |

Bu rakamları olduğu gibi oku: yaklaşık 62K token'lık tek bir küçük oturum. Bu boyutta token farkı ihmal edilebilir düzeydedir ve normal özet de pinlenen bilgileri tesadüfen tutmuştu. Boyut karşılaştırması, yan yana ölçmediğim uzun oturumlar için kesin bir şey söylemez.

Silme listesi de modele bağlıdır. Mythos 5.2 ile hem işaretlenen içerik hem işaretin kendisi özette yoktu. Haiku 5.5, neredeyse boş bir oturumdaki üç koşuda içeriği dışarıda bıraktı ama işareti alıntılayarak "bu madde özetten çıkarıldı" diye not düştü. Bedeli tek satırdır ve `drop_leaks` içinde sayılır.

İşaretleme modele bağlıdır. Mythos 5.2 her koşuda kurala uydu. Haiku 5.5 tutarsızdı: bir koşuda iki pin, bir başkasında hiç. Kuralın ifadesi de belirleyicidir: daha temkinli bir önceki ifade ("emin değilsen pinleme") iki modelde de hiç pin üretmedi.

Denenmeyenler: gerçek oturumda Linux ve Windows (birim testleri CI'da Linux üzerinde çalışır), tmux ve iTerm2 dışındaki terminaller, akıllı oto açıkken normal otomatik compact'ın kendi sınırında devreye girmesi.

## Dosyalar

| Yol | İçerik |
| :- | :- |
| `~/.claude/hooks/smart-compact.py` | Hook betiği. Standart kütüphane dışında bağımlılığı yok |
| `~/.claude/smart-compact/log.jsonl` | Her olay için bir satır: pinler, silme işaretleri, context boyutu, özet boyutu |
| `~/.claude/smart-compact/summaries/` | Her compact özeti; politikanın ne yaptığını görmek için |
| `~/.claude/smart-compact/pins/`, `manual/`, `cfg/`, `run/` | Oturuma özel durum |

30 günden eski oturum durumu kendiliğinden silinir. Hook içindeki her hata 0 ile çıkar ve kurulan hook komutları `|| true` ile biter; yani bozuk bir kurulum compact'ı engelleyemez. Geri yükleme için ayrılan 8.000 karaktere sığmayacak kadar çok pin varsa, en eskileri özete kelimesi kelimesine kopyalanmak üzere özeti yazan modele verilir.

## Geliştirme

```bash
python3 -m unittest discover -s tests
```

Testler betiği Claude Code'un çalıştırdığı gibi çalıştırır: hook JSON'u stdin'den, geçici bir ayar dizinine karşı.

## Lisans

[MIT](LICENSE)
