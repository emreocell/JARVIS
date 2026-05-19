# helpers/macos_legacy

Bu klasör, JARVIS v1 macOS sürümünden geriye kalan yardımcı dosyaları içerir.
JARVIS v2 yalnızca **Windows** üzerinde çalışır ve aşağıdaki dosyalar Windows
yapısı tarafından **kullanılmaz**:

- `jarvis_calendar_helper.swift`, `jarvis_calendar_helper.plist`
  - macOS EventKit üzerinden Apple Calendar / Apple Hatırlatıcılar erişimi
    sağlayan eski Swift CLI'sı.
- `jarvis_screen_helper.swift`, `jarvis_screen_helper.plist`
  - macOS `CGWindowList` + `screencapture` kullanarak aktif pencerenin
    ekran görüntüsünü alan eski Swift CLI'sı.
- `bin/jarvis-calendar-helper`
  - Yukarıdaki Swift kaynaktan derlenmiş, sadece macOS'ta çalışan ikili dosya.

## Neden hâlâ depoda?

- Tarihsel referans amacıyla saklanıyorlar; ileride macOS'a geri dönülürse
  EventKit / ScreenCapture entegrasyonu için başlangıç noktası olabilirler.
- Windows uyum katmanı (Outlook COM, `pywin32` ekran yakalama) bu dosyaların
  yerini aldığı için ana kod yolları artık `helpers/macos_legacy/` altındaki
  hiçbir dosyaya **referans vermez**.

## Windows kullanıcıları için uyarı

Bu klasördeki hiçbir dosyayı çalıştırmaya gerek yoktur ve `.swift` / `.plist`
dosyaları Windows üzerinde derlenemez. macOS binary (`bin/jarvis-calendar-helper`)
Mach-O formatındadır ve Windows'ta çalıştırılamaz.

Bu dosyalar repo'dan tamamen kaldırılmak istenirse `helpers/macos_legacy/`
klasörünün tümü güvenle silinebilir; v2 Windows kod tabanı bundan etkilenmez.
