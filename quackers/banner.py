"""ASCII-art banner shown when the interactive `quackers` session starts.

Font: FIGlet "ANSI Shadow" (blocky 3-D look), embedded as a constant so the
project needs no figlet dependency. `render()` paints the face in bright
cyan and any drop-shade row in dim cyan, giving the classic ANSI-Shadow
"depth" feel in the terminal.
"""
from __future__ import annotations

from typing import List

from quackers import term

TITLE = "HireMeQuackers"

# FIGlet stock font: ANSI Shadow. Five 7-char-tall glyph rows.
ART: list[str] = [
    r"██╗  ██╗██╗██████╗ ███████╗███╗   ███╗███████╗",
    r"██║  ██║██║██╔══██╗██╔════╝████╗ ████║██╔════╝",
    r"███████║██║██████╔╝█████╗  ██╔████╔██║█████╗  ",
    r"██╔══██║██║██╔══██╗██╔══╝  ██║╚██╔╝██║██╔══╝  ",
    r"██║  ██║██║██║  ██║███████╗██║ ╚═╝ ██║███████╗",
    r"╚═╝  ╚═╝╚═╝╚═╝  ╚═╝╚══════╝╚═╝     ╚═╝╚══════╝",
    r"",
    r" ██████╗ ██╗   ██╗ █████╗  ██████╗██╗  ██╗███████╗██████╗ ███████╗",
    r"██╔═══██╗██║   ██║██╔══██╗██╔════╝██║ ██╔╝██╔════╝██╔══██╗██╔════╝",
    r"██║   ██║██║   ██║███████║██║     █████╔╝ █████╗  ██████╔╝███████╗",
    r"██║▄▄ ██║██║   ██║██╔══██║██║     ██╔═██╗ ██╔══╝  ██╔══██╗╚════██║",
    r"╚██████╔╝╚██████╔╝██║  ██║╚██████╗██║  ██╗███████╗██║  ██║███████║",
    r" ╚══▀▀═╝  ╚═════╝ ╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚══════╝",
]

ART_WIDTH = max(len(line.rstrip()) for line in ART)


def render(width: int) -> List[str]:
    """Banner lines for a terminal `width` columns wide (plain title if too narrow).

    Colour is applied by `term.style`, which emits nothing when colours are
    off (pipes, NO_COLOR, unsupported consoles).
    """
    if width < ART_WIDTH + 2:
        return [term.style(TITLE, "bold", "bright_cyan")]
    return [term.style(line.rstrip(), "bold", "bright_cyan") for line in ART]
