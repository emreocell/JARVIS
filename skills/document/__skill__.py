"""Document skill manifest.

Plugin_Host bu modülü keşfeder, MANIFEST global'ını okur ve
document_qa.py içindeki handler'ı Tool_Runtime'a kaydeder.
"""

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST = SkillManifest(
    name="document",
    version="0.1.0",
    enabled=True,
    entry_module="skills.document.document_qa",
    tools=[
        "document_qa",
    ],
    description=(
        "PDF, DOCX, TXT ve MD dosyalarını okuyup soru-cevap yapar. "
        "Büyük dosyalar otomatik olarak bölümlere ayrılır."
    ),
    requires=["pypdf", "docx"],
)


__all__ = ["MANIFEST"]
