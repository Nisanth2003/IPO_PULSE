"""IPO Pulse — data layer for the reel studio.

Numbers and prose live here; frontend/ only visualises what this publishes.
"""

__version__ = "1.0.0"

from .models import Ipo
from .compute import derive

__all__ = ["Ipo", "derive", "__version__"]
