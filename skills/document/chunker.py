"""Chunking helper for the Document_QA skill.

Bu modül `chunk(text, max_chunk_size)` fonksiyonuyla bir belge metnini
sırayla okunabilen alt parçalara böler (Requirement 20.2, Design §8).
``Document_QA`` 200 sayfayı aşan belgelerde her parçayı ayrı bir özet
çağrısıyla işler; bu modül o boru hattının ilk adımıdır.

Sözleşme (Property 20: "Document_QA chunking bütünlüğü"):

    1. Sıralı concatenation orijinal metne eşittir
       (``"".join(chunk(text, n)) == text``).
    2. Her chunk uzunluğu ``max_chunk_size`` değerini aşmaz.
    3. ``text == ""`` ise sonuç boş listedir.
    4. ``max_chunk_size`` pozitif bir tamsayı olmalıdır; aksi halde
       ``ValueError``.

Bu sözleşme **tek karakter bile kaybetmediğimizi ya da
çoğaltmadığımızı** garanti eder; bu nedenle "overlap" stratejisi
desteklenmez (overlap, concatenation eşitliğini bozar).

Strateji:

* Sözleşmeyi tutarken **kelime sınırlarını tercih ederiz**: pencere
  içindeki son whitespace karakterinden hemen sonra kesim yapılır;
  whitespace pencerede bulunamazsa veya çok başta kalıyorsa
  sert (karakter-tabanlı) kesim uygulanır.
* "Çok başta" eşiği pencerenin yarısıdır — bu sayede tek kelimelik
  cümleler veya whitespace'siz uzun bloklar (örn. base64) dengeli
  parçalara bölünür ve aşırı küçük chunk'lar oluşmaz.

Sözleşme deterministiktir; aynı (``text``, ``max_chunk_size``) çifti
her zaman aynı listeyi üretir.
"""

from __future__ import annotations

from typing import List


def _validate_size(max_chunk_size: int) -> None:
    """``max_chunk_size`` pozitif tamsayı mı doğrula."""

    # ``bool`` ``int``'in alt sınıfıdır; True/False parametre olarak
    # geçirilirse sessizce 1/0 olarak işlem görür. Bunu açıkça reddet.
    if isinstance(max_chunk_size, bool) or not isinstance(max_chunk_size, int):
        raise ValueError(
            "max_chunk_size pozitif bir tamsayı olmalı, "
            f"alındı: {type(max_chunk_size).__name__}"
        )
    if max_chunk_size <= 0:
        raise ValueError(
            f"max_chunk_size pozitif olmalı, alındı: {max_chunk_size}"
        )


def chunk(text: str, max_chunk_size: int) -> List[str]:
    """Bir metni en fazla ``max_chunk_size`` uzunluğunda parçalara böl.

    Args:
        text: Bölünecek belge metni. Boş string için boş liste döner.
        max_chunk_size: Her parçanın azami karakter uzunluğu. Pozitif
            tamsayı olmalı.

    Returns:
        ``text``'in sıralı parçalarının listesi. ``"".join(...)``
        orijinal metne eşittir ve her elemanın uzunluğu
        ``max_chunk_size`` değerini aşmaz.

    Raises:
        ValueError: ``max_chunk_size`` pozitif tamsayı değilse.

    Property:
        Property 20 — Document_QA chunking bütünlüğü
        (Validates: Requirements 20.2).
    """

    _validate_size(max_chunk_size)

    if text == "":
        return []

    chunks: List[str] = []
    pos = 0
    n = len(text)

    # Whitespace tercih eşiği: pencerenin yarısından önceki kesimleri
    # kabul etmeyiz; aksi halde "a b c d" gibi metinler tek karakterlik
    # parçalara bölünebilir. ``max_chunk_size == 1`` özel durumunda
    # threshold = pos + 1 olur ve hiçbir whitespace kabul edilmez —
    # bu istenen davranış (her chunk tam 1 karakter).
    while pos < n:
        hard_end = pos + max_chunk_size
        if hard_end >= n:
            # Son parça — kalan tüm metni al.
            chunks.append(text[pos:n])
            break

        # Pencere = text[pos:hard_end]. Sağdan sola tarayıp whitespace
        # ararız; bulunan ilk whitespace pencere yarısından önce ise
        # kabul etmeyiz ve sert kesim yaparız.
        threshold = pos + (max_chunk_size // 2)
        boundary = -1
        # range(hard_end - 1, threshold - 1, -1): sağ kenardan başlayıp
        # threshold (hariç tutulmayan) konumuna kadar geriye doğru.
        for i in range(hard_end - 1, threshold - 1, -1):
            if text[i].isspace():
                # Whitespace karakterini bu chunk'ın içinde tut;
                # böylece sonraki chunk whitespace olmadan başlar ve
                # toplam karakter sayısı korunur.
                boundary = i + 1
                break

        end = boundary if boundary != -1 else hard_end
        chunks.append(text[pos:end])
        pos = end

    return chunks


__all__ = ["chunk"]
