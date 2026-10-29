"""ASCII-art banner shown when the interactive `quackers` session starts.

The art is FIGlet's "standard" font with its default smushing rules,
embedded as a constant so the project needs no figlet dependency.
"""
from __future__ import annotations

from typing import List

from quackers import term

TITLE = "HireMeQuackers"

ART: List[str] = [
    r" _   _ _          __  __       ___                   _",
    r"| | | (_)_ __ ___|  \/  | ___ / _ \ _   _  __ _  ___| | _____ _ __ ___",
    r"| |_| | | '__/ _ \ |\/| |/ _ \ | | | | | |/ _` |/ __| |/ / _ \ '__/ __|",
    r"|  _  | | | |  __/ |  | |  __/ |_| | |_| | (_| | (__|   <  __/ |  \__ \ ",
    r"|_| |_|_|_|  \___|_|  |_|\___|\__\_\\__,_|\__,_|\___|_|\_\___|_|  |___/",
]

ART_WIDTH = max(len(line.rstrip()) for line in ART)


def render(width: int) -> List[str]:
    """Banner lines for a terminal `width` columns wide (plain title if too narrow).

    Colour is applied by `term.style`, which emits nothing when colours are
    off (pipes, NO_COLOR, unsupported consoles).
    """
    if width < ART_WIDTH + 2:
        return [term.style(TITLE, "bold", "cyan")]
    return [term.style(line.rstrip(), "bold", "cyan") for line in ART]
