"""Tool_Runtime — Gemini Live tool dispatcher.

Owns the registry of :class:`runtime.types.ToolDescriptor` records and
routes every Gemini ``tool_call`` to either an inline handler (executed
synchronously on a worker thread via :func:`asyncio.to_thread`) or to the
:class:`runtime.task_manager.TaskManager` for background execution.

Design references
-----------------
- ``design.md`` § "Tool_Runtime" defines the public surface and the
  inline / background dispatch contract.
- Requirement 2 — every tool carries an ``execution_mode``, the slow
  NVIDIA / document / email tools default to ``"background"``, and
  background dispatch must hand a ``tool_response`` back to Voice_Core
  within 5 seconds (Req 2.5) so the Gemini Live oturumu konuşmaya devam
  edebilsin.
- Requirement 15 — inline ``PermissionError`` / Win32 elevation hataları
  :mod:`runtime.uac_translator` ile Türkçe kullanıcı mesajına çevrilir.
- Requirement 1.8, 15.5, 15.6 — Model_Router aynı örnek hem inline hem
  background handler'larına enjekte edilir; ``requests.ConnectionError``
  Türkçe paragraf mesajına çevrilir.

Dispatch contract
-----------------
``dispatch`` always returns a ``dict`` payload that the caller (Voice_Core)
folds into ``types.FunctionResponse.response``.

* Inline başarı: ``{"result": "<handler output>"}``.
* Inline hata (UAC çevrildi): ``{"result": "<Türkçe mesaj>"}``.
* Inline hata (bağlantı hatası): ``{"result": "İnternet bağlantısında sorun var..."}``.
* Inline hata (UAC dışı): ``{"result": "<ExceptionType>: <message>"}``.
* Background dispatch: ``{"task_id": "<id>", "status": "queued",
  "message": "Görev arka planda başlatıldı, kabul edildi."}`` —
  Requirement 2.4 + 2.5'in 5 saniyelik kabul süresi.
* Bilinmeyen tool: ``{"result": "Bilinmeyen tool: <name>"}``.

Handler context injection
-------------------------
Handlers that accept ``model_router`` and/or ``privacy_mode`` keyword
arguments will receive the runtime's instances automatically. Handlers
that do not declare these parameters are called without them, preserving
full backward compatibility.

The runtime is intentionally thin: skill yükleme :class:`PluginHost`'un,
hata duyurma Result_Announcer'ın sorumluluğunda. ``ToolRuntime`` yalnızca
"hangi tool, nereye gider" sorusunu yanıtlar.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import TYPE_CHECKING, Any, Final

import requests

from runtime import uac_translator
from runtime.safety_gate import block_message, evaluate_tool_call
from runtime.task_manager import TaskManager
from runtime.types import BackgroundTask, ExecutionMode, ToolDescriptor

if TYPE_CHECKING:
    from runtime.privacy_mode import PrivacyMode

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Turkish connection error message (Req 15.5, 15.6)
# ---------------------------------------------------------------------------

#: Türkçe bağlantı hatası mesajı; ``requests.ConnectionError`` yakalandığında
#: kullanıcıya bu paragraf döner.
CONNECTION_ERROR_MESSAGE_TR: Final[str] = (
    "İnternet bağlantısında sorun var, lütfen bağlantınızı kontrol edip tekrar deneyin."
)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------


#: Tools that default to ``"background"`` execution mode (Req 2.2).
#:
#: ``send_email`` Outlook COM açılışı yavaş olabildiği için, ``document_qa``
#: ise > 200 sayfa branch'inde dakikalar sürebileceği için listededir.
DEFAULT_BACKGROUND_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "video_object_detect",
        "audio_to_table",
        "nvidia_text_task",
        "nvidia_image_analyze",
        "document_qa",
        "send_email",
    }
)

#: Background dispatch acknowledgement budget (Req 2.5). The submit step is
#: effectively non-blocking, ama defansif olarak ``asyncio.wait_for``
#: kuyruğa çok uzun süre bekletilen bir submit'i 5 sn içinde keser.
BACKGROUND_ACK_DEADLINE_SEC: Final[float] = 5.0

#: Türkçe kabul mesajı; Voice_Core bunu konuşmaya çevirir, kullanıcı
#: kuyruğun başladığını anlar.
BACKGROUND_ACK_MESSAGE: Final[str] = (
    "Görev arka planda başlatıldı, kabul edildi."
)


def default_execution_mode_for(name: str) -> ExecutionMode:
    """Return the default execution mode for ``name``.

    Returns ``"background"`` for tools listed in
    :data:`DEFAULT_BACKGROUND_TOOLS`, ``"inline"`` for everything else.
    Skill yazarları kayıt sırasında bu yardımcıyı çağırarak tutarlı
    varsayılanlar elde eder; manifesto açıkça başka bir mod belirtirse
    onu kullanmaları yeterli.
    """
    return "background" if name in DEFAULT_BACKGROUND_TOOLS else "inline"


# ---------------------------------------------------------------------------
# ToolRuntime
# ---------------------------------------------------------------------------


class ToolRuntime:
    """Generic dispatcher between Voice_Core ve skill handler'ları.

    Parameters
    ----------
    task_manager:
        :class:`TaskManager` instance; ``"background"`` modlu tool'lar
        bu yöneticiye delege edilir.
    voice:
        Optional Voice_Core handle. Şu an depolanır ama dispatch
        tarafından kullanılmaz; skill handler'ları için ileride hook
        zinciri hazırlığıdır.
    model_router:
        Optional :class:`ModelRouter` instance (Req 1.8). Hem inline hem
        background handler'larına enjekte edilir. Handler imzasında
        ``model_router`` parametresi varsa otomatik geçirilir; yoksa
        çağrı etkilenmez (geriye uyumluluk korunur).
    privacy_mode:
        Optional :class:`PrivacyMode` instance. Handler imzasında
        ``privacy_mode`` parametresi varsa otomatik geçirilir.
    """

    def __init__(
        self,
        task_manager: TaskManager,
        *,
        voice: Any = None,
        model_router: Any = None,
        privacy_mode: PrivacyMode | None = None,
    ) -> None:
        self._task_manager = task_manager
        self.voice = voice
        self.model_router = model_router
        self.privacy_mode = privacy_mode
        self._tools: dict[str, ToolDescriptor] = {}

    # ------------------------------------------------------------ registry

    def register(self, descriptor: ToolDescriptor) -> None:
        """Register ``descriptor``. Aynı isim daha önce kayıtlıysa raises.

        Plugin_Host bir skill'i reload ederken önce ``unregister`` çağırır;
        bu yüzden ``register`` tarafında çift kayıt sessizce kabul edilemez.
        """
        if not descriptor.name:
            raise ValueError("ToolDescriptor.name must be a non-empty string")
        if descriptor.name in self._tools:
            raise ValueError(
                f"Tool {descriptor.name!r} is already registered; "
                "unregister it first or pick a different name."
            )
        self._tools[descriptor.name] = descriptor

    def unregister(self, name: str) -> None:
        """Remove ``name`` from the registry.

        Bilinmeyen isimler sessizce yok sayılır; Plugin_Host reload
        akışında "kayıtlı olmayabilir" durumu normaldir.
        """
        self._tools.pop(name, None)

    def get(self, name: str) -> ToolDescriptor | None:
        """Return the descriptor for ``name`` or ``None`` if absent."""
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        """Return registered tool names in registration order."""
        return list(self._tools.keys())

    def declarations(self) -> list[dict]:
        """Gemini Live ``tools=[...]`` payload'u için declaration listesi.

        Liste her çağrıda yeni bir Python list oluşturarak kayıt
        tablosundan kopyalanır; çağıran taraf bu listeyi serbestçe
        değiştirebilir, runtime tablosu etkilenmez.
        """
        return [desc.declaration for desc in self._tools.values()]

    def __contains__(self, name: object) -> bool:  # küçük ergonomi
        return isinstance(name, str) and name in self._tools

    # ---------------------------------------------------------- dispatch

    async def dispatch(
        self,
        name: str,
        args: dict | None = None,
        *,
        voice: Any = None,
    ) -> dict:
        """Tek bir tool çağrısını yürüt ve tool_response payload'unu döner.

        Parameters
        ----------
        name:
            Çağrılan tool adı (Gemini ``function_call.name``).
        args:
            Gemini'nin ürettiği argüman sözlüğü. ``None`` boş dict gibi
            ele alınır.
        voice:
            Çağrı başına Voice_Core override'ı. Verilmezse runtime'ın
            kurulumda aldığı handle kullanılır. Şu anki implementasyon
            bu değeri yalnızca instance üzerinde günceller; ileride
            handler'lara geçirilebilir.

        Returns
        -------
        dict
            Yukarıdaki "Dispatch contract" bölümünde tarif edilen şekil.
        """
        call_args = dict(args or {})
        if voice is not None:
            self.voice = voice

        descriptor = self._tools.get(name)
        if descriptor is None:
            log.warning("ToolRuntime: unknown tool %r dispatched", name)
            return {"result": f"Bilinmeyen tool: {name}"}

        safety = evaluate_tool_call(name, call_args, self.model_router)
        if str(safety.get("decision", "")).lower() in {"ask_user", "stop"}:
            log.info("ToolRuntime: blocked risky tool call %s safety=%s", name, safety)
            return {
                "result": block_message(name, safety),
                "safety": safety,
                "blocked": True,
            }

        if descriptor.execution_mode == "background":
            return await self._dispatch_background(descriptor, call_args)
        return await self._dispatch_inline(descriptor, call_args)

    # =========================================================== internals

    async def _dispatch_inline(
        self, descriptor: ToolDescriptor, args: dict
    ) -> dict:
        """Inline yürütme: handler'ı thread havuzunda çağır, hatayı çevir."""
        call_kwargs = self._inject_context(descriptor.handler, args)
        try:
            result = await asyncio.to_thread(descriptor.handler, **call_kwargs)
        except requests.exceptions.ConnectionError as exc:
            log.warning(
                "ToolRuntime: connection error in inline tool %r: %s",
                descriptor.name,
                exc,
            )
            return {"result": CONNECTION_ERROR_MESSAGE_TR}
        except BaseException as exc:  # noqa: BLE001 — UAC çevirisi için tüm hata yelpazesi
            return {"result": self._format_inline_error(descriptor.name, exc)}

        # ``None`` döndüren handler'lar için kullanıcıya boş string yerine
        # nötr bir geri bildirim sun: "Tamamlandı." ya benzeri tartışmaya
        # açık olabileceği için boş string'i koruyup üst katmana bırakıyoruz.
        return {"result": "" if result is None else str(result)}

    @staticmethod
    def _format_inline_error(tool_name: str, exc: BaseException) -> str:
        """UAC çevirisini dene, olmazsa generic exception formatına düş."""
        try:
            translated = uac_translator.translate(exc, tool_name=tool_name)
        except Exception:  # pragma: no cover - translator must never break dispatch
            log.exception("uac_translator.translate raised for tool %s", tool_name)
            translated = None

        if translated:
            return translated
        return f"{type(exc).__name__}: {exc}"

    def _inject_context(self, handler: Any, args: dict) -> dict:
        """Handler imzasına göre ``model_router`` ve ``privacy_mode`` enjekte et.

        Handler imzasında ``model_router`` veya ``privacy_mode`` parametresi
        varsa runtime'ın tuttuğu örnekler eklenir. Parametre yoksa args
        değişmeden döner — geriye uyumluluk korunur (Req 1.8).

        Parameters
        ----------
        handler:
            Çağrılacak Python callable.
        args:
            Gemini'den gelen argüman sözlüğü (değiştirilmez; kopya döner).

        Returns
        -------
        dict
            Gerekirse ``model_router`` ve/veya ``privacy_mode`` eklenmiş
            argüman sözlüğü.
        """
        try:
            sig = inspect.signature(handler)
            params = sig.parameters
        except (ValueError, TypeError):
            # İmza alınamayan callable'lar (C extension vb.) için güvenli düşüş.
            return dict(args)

        injected = dict(args)
        if "model_router" in params and self.model_router is not None:
            injected.setdefault("model_router", self.model_router)
        if "privacy_mode" in params and self.privacy_mode is not None:
            injected.setdefault("privacy_mode", self.privacy_mode)
        if "tool_runtime" in params:
            injected.setdefault("tool_runtime", self)
        return injected

    async def _dispatch_background(
        self, descriptor: ToolDescriptor, args: dict
    ) -> dict:
        """Background yürütme: TaskManager'a submit et, kabul ack'i döner."""
        # Capture context references for the closure (Req 1.8): both inline
        # and background tasks share the same Model_Router instance.
        call_kwargs = self._inject_context(descriptor.handler, args)

        def _wrapper(task: BackgroundTask) -> str:
            """ThreadPoolExecutor içinde çalışan sarmalayıcı.

            ``task.cancel_event`` early-exit kontrolü için sağlanır;
            handler imzası ``cancel_event``'i kabul ediyorsa ona
            geçirilir, etmiyorsa yalnızca ``args`` kullanılır. Çift
            kontrol: handler tarafının cooperatif iptale destek vermesi
            zorunlu değil; handler bitse bile TaskManager event set ise
            sonucu cancelled olarak işaretler.
            """
            if task.cancel_event.is_set():
                # Handler'a girmeden iptal edilmiş; TaskManager bu durumu
                # finalise eder, biz sadece erken çıkıyoruz.
                return ""
            try:
                result = descriptor.handler(**call_kwargs)
            except requests.exceptions.ConnectionError as exc:
                log.warning(
                    "ToolRuntime: connection error in background tool %r: %s",
                    descriptor.name,
                    exc,
                )
                return CONNECTION_ERROR_MESSAGE_TR
            return "" if result is None else str(result)

        # ``submit`` non-blocking; defansif wait_for sadece "TaskManager
        # kapatılmış olabilir mi?" gibi sürpriz durumları 5 sn içinde
        # raporlar (Req 2.5).
        try:
            task = await asyncio.wait_for(
                asyncio.to_thread(
                    self._task_manager.submit,
                    descriptor.name,
                    _wrapper,
                    args,
                    skill_id=descriptor.skill_id,
                ),
                timeout=BACKGROUND_ACK_DEADLINE_SEC,
            )
        except asyncio.TimeoutError:
            log.error(
                "ToolRuntime: TaskManager.submit for %s exceeded %.1fs ack budget",
                descriptor.name,
                BACKGROUND_ACK_DEADLINE_SEC,
            )
            return {
                "result": (
                    "Görev kuyruğa alınamadı: arka plan yöneticisi yanıt vermiyor."
                )
            }
        except Exception as exc:  # noqa: BLE001 — submit hataları kullanıcıya net dönmeli
            log.exception(
                "ToolRuntime: TaskManager.submit raised for %s", descriptor.name
            )
            return {"result": f"Görev kuyruğa alınamadı: {type(exc).__name__}: {exc}"}

        return {
            "task_id": task.id,
            "status": "queued",
            "message": BACKGROUND_ACK_MESSAGE,
        }


__all__ = [
    "ToolRuntime",
    "DEFAULT_BACKGROUND_TOOLS",
    "BACKGROUND_ACK_DEADLINE_SEC",
    "BACKGROUND_ACK_MESSAGE",
    "CONNECTION_ERROR_MESSAGE_TR",
    "default_execution_mode_for",
]
