"""Built-in inline tools published directly by the Tool_Runtime.

Bu modül, herhangi bir skill paketi içinde yer almayan ama her oturumda
mevcut olması gereken iki yerleşik inline tool'u tanımlar:

* ``list_background_tasks`` — kullanıcının "görev nasıl gidiyor?" sorusuna
  cevap veren, son 30 dakikadaki Background_Task'lerin id, ad, durum ve
  geçen süre özetini döner (Req 4.1, 4.2).
* ``cancel_background_task`` — verilen kimliğe sahip görevi iptal etmeye
  çalışır. Görev terminal durumdaysa (succeeded / failed / cancelled /
  announced) durumunu değiştirmez ve mevcut durumu raporlar (Req 4.3,
  4.4, 4.5).

Bu tool'lar Plugin_Host üzerinden yüklenmez; ``main.py`` bootstrap
sırasında :func:`register_builtin_tools` çağırarak Tool_Runtime'a doğrudan
kaydeder. Skill kimliği olarak :data:`BUILTIN_SKILL_ID` (`"_builtin"`)
kullanılır; başına alt çizgi koymak Plugin_Host'un keşif filtresinin
("alt çizgi ile başlayanları atla") aynı ad altında bir paketi yanlışlıkla
yüklemesini önler.

Tasarım notları
---------------
* Her iki tool ``inline`` modundadır; çağrıları doğrudan Task_Manager'a
  hızlı sorgular olarak iner ve Voice_Core'u bloklamaz.
* Handler'lar ``TaskManager`` referansını closure üzerinden yakalar; bu
  sayede aynı süreçte birden fazla TaskManager bulunsa bile (örn.
  testlerde) doğru olanla konuşurlar.
* Cancel tarafı **idempotent**'tir: terminal görev için "zaten sonlanmış"
  metni döner ve hiçbir alanı (state, result_text, error_text) değiştirmez
  (Req 4.5). Get → cancel arasında bir yarış görev bitirirse bu da
  defansif olarak ele alınır.
"""

from __future__ import annotations

import logging
from typing import Final

from runtime.task_manager import TaskManager
from runtime.tool_runtime import ToolRuntime
from runtime.types import BackgroundTask, ToolDescriptor

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------


#: Yerleşik tool'ların ``ToolDescriptor.skill_id`` alanına yazılan değer.
#:
#: Plugin_Host alt çizgi ile başlayan klasörleri atladığı için (bkz.
#: ``runtime/plugin_host.py::PluginHost.discover``) bu ad disk üzerinde
#: bir skill paketi tarafından çakıştırılamaz.
BUILTIN_SKILL_ID: Final[str] = "_builtin"

#: ``list_background_tasks`` zaman penceresi (Req 4.2).
RECENT_WINDOW_MINUTES: Final[int] = 30


# ---------------------------------------------------------------------------
# Gemini declarations
# ---------------------------------------------------------------------------


#: ``list_background_tasks`` argümansızdır; Gemini şemasında
#: ``parameters`` alanı atlanır (Plugin_Host doğrulayıcısı bu durumu
#: açıkça destekler).
LIST_BACKGROUND_TASKS_DECLARATION: Final[dict] = {
    "name": "list_background_tasks",
    "description": (
        "Son 30 dakika içinde başlatılmış arka plan görevlerini "
        "kimliği, adı, durumu ve geçen süresiyle listeler. "
        "Kullanıcı 'görevlerim ne oldu?' veya 'arka planda ne çalışıyor?' "
        "diye sorduğunda kullan."
    ),
}

CANCEL_BACKGROUND_TASK_DECLARATION: Final[dict] = {
    "name": "cancel_background_task",
    "description": (
        "Verilen kimliğe sahip arka plan görevini iptal etmeye çalışır. "
        "Görev henüz tamamlanmadıysa durumu 'cancelled' olarak işaretlenir; "
        "zaten tamamlanmış, başarısız olmuş veya iptal edilmişse hiçbir "
        "değişiklik yapılmaz ve mevcut durum raporlanır."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "id": {
                "type": "STRING",
                "description": (
                    "İptal edilecek Background_Task kimliği. "
                    "list_background_tasks çıktısında her satırın başında yer alır."
                ),
            },
        },
        "required": ["id"],
    },
}


# ---------------------------------------------------------------------------
# Handler implementations
# ---------------------------------------------------------------------------


def _format_task_summary(task: BackgroundTask) -> str:
    """Tek satırlık özet üretir: ``<id> <name> [<state>] <elapsed>s``.

    Geçen süre :meth:`BackgroundTask.elapsed_seconds` üzerinden okunur;
    görev henüz başlamadıysa ``created_at``'tan, başladıysa
    ``started_at``'tan, bittiyse ``finished_at``'a kadar olan farkı verir.
    """
    elapsed = task.elapsed_seconds()
    return f"{task.id} {task.name} [{task.state}] {elapsed:.1f}s"


def _make_list_handler(task_manager: TaskManager):
    """Closure ile ``list_background_tasks`` handler'ını oluştur.

    Handler, ``TaskManager.list_recent`` çıktısını insan tarafından
    okunabilir, çoklu satırlık bir özet metne çevirir. Boş kuyruk için
    kullanıcıya net bir geri bildirim verir; aksi halde Voice_Core
    boş string okumak zorunda kalırdı.
    """

    def list_background_tasks() -> str:
        tasks = task_manager.list_recent(RECENT_WINDOW_MINUTES)
        if not tasks:
            return "Son 30 dakikada arka plan görevi yok."
        lines = [_format_task_summary(t) for t in tasks]
        return "Son 30 dakikadaki görevler:\n" + "\n".join(lines)

    # Handler'ın __name__'i declaration ile birebir tutulur; loglarda ve
    # ToolRuntime hata mesajlarında karışıklığı önler.
    list_background_tasks.__name__ = "list_background_tasks"
    list_background_tasks.__qualname__ = "list_background_tasks"
    return list_background_tasks


def _make_cancel_handler(task_manager: TaskManager):
    """Closure ile ``cancel_background_task`` handler'ını oluştur.

    Davranış (Req 4.4, 4.5):

    * Bilinmeyen kimlik → "Bilinmeyen görev kimliği" mesajı, mutasyon yok.
    * Terminal görev → mevcut durum raporlanır, `state/result_text/
      error_text` değişmez.
    * Aktif görev (queued/running) → ``TaskManager.cancel`` çağrılır;
      başarılıysa onay mesajı döner. ``get → cancel`` arasında yarışla
      görev terminal duruma geçtiyse bu da defansif olarak raporlanır.
    """

    def cancel_background_task(id: str) -> str:
        if not isinstance(id, str) or not id.strip():
            return "Geçersiz görev kimliği: boş veya string değil."

        task_id = id.strip()
        task = task_manager.get(task_id)
        if task is None:
            return f"Bilinmeyen görev kimliği: {task_id}"

        if task.is_terminal:
            # Req 4.5 — hiçbir alanı değiştirmeden mevcut durumu raporla.
            return (
                f"{task_id} adlı görev zaten sonlanmış "
                f"(durum: {task.state}); değişiklik yapılmadı."
            )

        cancelled = task_manager.cancel(task_id)
        if cancelled:
            return f"{task_id} adlı görev iptal edildi."

        # ``cancel`` False döndürdü; aralarda terminal duruma geçtiği için
        # bilgi mesajı veriyoruz, mutasyon riski yok.
        current = task_manager.get(task_id)
        state = current.state if current is not None else "bilinmiyor"
        return (
            f"{task_id} adlı görev iptal edilmedi; "
            f"mevcut durum: {state}."
        )

    cancel_background_task.__name__ = "cancel_background_task"
    cancel_background_task.__qualname__ = "cancel_background_task"
    return cancel_background_task


# ---------------------------------------------------------------------------
# Registration entry point
# ---------------------------------------------------------------------------


def register_builtin_tools(
    runtime: ToolRuntime,
    task_manager: TaskManager,
) -> list[ToolDescriptor]:
    """Yerleşik inline tool'ları ``runtime`` üzerine kaydet.

    Çağrı bootstrap sırasında bir kez yapılır (genelde
    ``main.py::JarvisLive.__init__`` içinde, Plugin_Host yüklemesinden
    önce veya sonra fark etmez — isim çakışması olmaz).

    Returns
    -------
    list[ToolDescriptor]
        Kaydedilen iki descriptor; çağıran taraf isterse sonradan
        ``runtime.unregister(...)`` ile kaldırabilir.
    """
    list_handler = _make_list_handler(task_manager)
    cancel_handler = _make_cancel_handler(task_manager)

    descriptors = [
        ToolDescriptor(
            name="list_background_tasks",
            declaration=LIST_BACKGROUND_TASKS_DECLARATION,
            handler=list_handler,
            execution_mode="inline",
            skill_id=BUILTIN_SKILL_ID,
        ),
        ToolDescriptor(
            name="cancel_background_task",
            declaration=CANCEL_BACKGROUND_TASK_DECLARATION,
            handler=cancel_handler,
            execution_mode="inline",
            skill_id=BUILTIN_SKILL_ID,
        ),
    ]
    for desc in descriptors:
        runtime.register(desc)
        log.debug(
            "register_builtin_tools: registered %r (skill_id=%s, mode=%s)",
            desc.name,
            desc.skill_id,
            desc.execution_mode,
        )
    return descriptors


__all__ = [
    "register_builtin_tools",
    "BUILTIN_SKILL_ID",
    "RECENT_WINDOW_MINUTES",
    "LIST_BACKGROUND_TASKS_DECLARATION",
    "CANCEL_BACKGROUND_TASK_DECLARATION",
]
