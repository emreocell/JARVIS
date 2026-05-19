"""Creative_Skill için saf yardımcı fonksiyonlar.

Bu modül HTTP, dosya I/O veya başka bir yan etki içermez. Yalnızca girdi
metnini deterministik biçimde dönüştürür, böylece property tabanlı testlerle
(Hypothesis) doğrulanabilir.

Kapsanan gereksinimler:
- Requirement 9.3: ``financial_analyze`` çıktısı "Bu yatırım tavsiyesi değildir"
  Türkçe uyarısıyla başlamak zorundadır.
- Requirement 9.4: ``medical_qa`` çıktısı "Bu profesyonel tıbbi tavsiye yerine
  geçmez, bir doktora danışın" Türkçe uyarısıyla başlamak zorundadır.
- Requirement 9.6: Kullanıcı uyarıyı kapatmak isterse bile (``suppress_disclaimer
  is True``) finansal ve tıbbi uyarılar kaldırılmaz; bu uyarılar kalıcı yasal
  gerekliliktir.
"""

from __future__ import annotations

from typing import Final, Literal

DisclaimerKind = Literal["financial", "medical", "creative"]

# Türkçe yasal uyarı metinleri. Tasarım dokümanındaki ifadelerle birebir aynıdır
# ve "kalıcı yasal gereklilik" olarak Property 17 tarafından korunur.
FINANCIAL_DISCLAIMER: Final[str] = "Bu yatırım tavsiyesi değildir."
MEDICAL_DISCLAIMER: Final[str] = (
    "Bu profesyonel tıbbi tavsiye yerine geçmez, bir doktora danışın."
)

# `kind` → uyarı metni eşlemesi. ``creative`` için uyarı yoktur ve
# ``suppress_disclaimer`` bayrağı bu durumda anlamlıdır.
_DISCLAIMERS: Final[dict[str, str]] = {
    "financial": FINANCIAL_DISCLAIMER,
    "medical": MEDICAL_DISCLAIMER,
}

# Finansal ve tıbbi uyarılar yasal olarak zorunludur ve
# ``suppress_disclaimer=True`` olsa bile kaldırılamaz (Req 9.6).
_MANDATORY_KINDS: Final[frozenset[str]] = frozenset({"financial", "medical"})

# Paragraf ayracı — Property 17, ``split("\n\n")[0]`` ile çıktı kontrolü yapar.
_PARAGRAPH_SEP: Final[str] = "\n\n"


def format_with_disclaimer(
    kind: str,
    text: str,
    suppress_disclaimer: bool = False,
) -> str:
    """Çıktının başına ``kind``'a uygun Türkçe yasal uyarıyı zorla ekler.

    Saf fonksiyon: aynı girdi her zaman aynı çıktıyı üretir; yan etki yoktur.

    Davranış:

    * ``kind == "financial"`` → çıktının ilk paragrafı
      :data:`FINANCIAL_DISCLAIMER` olur.
    * ``kind == "medical"`` → çıktının ilk paragrafı
      :data:`MEDICAL_DISCLAIMER` olur.
    * ``kind == "creative"`` → uyarı eklenmez; ``text`` olduğu gibi döner.
      Bu kategori için ``suppress_disclaimer`` parametresi anlamlıdır ancak
      zaten uyarı yokmuş gibi davranır.
    * Finansal ve tıbbi uyarılar ``suppress_disclaimer=True`` olsa bile
      asla kaldırılmaz (Req 9.6, Property 17).

    Args:
        kind: ``"financial"``, ``"medical"`` veya ``"creative"``.
        text: LLM ham yanıtı. Boş veya yalnızca boşluk içeren metinler de
            kabul edilir.
        suppress_disclaimer: ``True`` ise ``creative`` için uyarı eklenmez.
            Finansal/tıbbi kategorilerde bu bayrak yoksayılır.

    Returns:
        Uyarı (varsa) ilk paragraf olarak başa zorlanmış metin. Çıktı,
        ``\n\n`` ile ayrılmış paragraflar dizisidir; ilk paragraf uyarı,
        kalanı orijinal ``text``'in korunmuş içeriğidir.

    Raises:
        ValueError: ``kind`` desteklenen değerlerden biri değilse.
    """
    if kind not in _DISCLAIMERS and kind != "creative":
        raise ValueError(
            f"Desteklenmeyen disclaimer kind: {kind!r}. "
            "Beklenen değerler: 'financial', 'medical', 'creative'."
        )

    body = text if isinstance(text, str) else str(text)

    # Creative kategorisi için yasal uyarı yok; suppress_disclaimer bayrağı
    # bu noktada anlamlıdır ancak ekleyecek bir uyarı olmadığı için davranış
    # her iki değerde de aynıdır.
    if kind == "creative":
        return body

    disclaimer = _DISCLAIMERS[kind]

    # Finansal/tıbbi: suppress_disclaimer True olsa bile uyarı zorlanır.
    # _MANDATORY_KINDS kontrolü, ileride yeni bir kategori eklendiğinde
    # davranışın açıkça korunabilmesi için bir dizgindir.
    _ = kind in _MANDATORY_KINDS  # kalıcı yasal gereklilik (Req 9.6)

    # Çıktıyı paragraf paragraf inşa et: önce uyarı, sonra orijinal metin.
    # ``body`` boş veya yalnızca boşluksa, uyarıdan sonra ek paragraf
    # üretmiyoruz; böylece çıktı yalnızca uyarı paragrafı olur ve
    # ``split("\n\n")[0]`` yine uyarıyı verir.
    if body.strip() == "":
        return disclaimer

    return f"{disclaimer}{_PARAGRAPH_SEP}{body}"
