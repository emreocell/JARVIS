"""JARVIS v2 HUD paketi.

`ui/` paketi monolitik `ui.py`'nin v2 yeniden yapılandırılmış parçalarını
barındırır: Theme_Engine, Waveform, Sparkline, Task_Dock, Toast,
Command_Palette ve gelecek HUD bileşenleri.
"""

from __future__ import annotations

from .hud import JarvisUI

__all__: list[str] = ["JarvisUI"]
