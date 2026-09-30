"""MockCore — a deliberately hostile, fictional "legacy core banking" web app.

It is the stand-in target for the automation system. It mimics the properties of real
back-office bank software that make automation hard:

* server-rendered HTML 4 with a frameset (nav + main frames)
* table-based layout, no element ids, no test ids, labels that are not associated with inputs
* ``<span onclick>`` "buttons" and ``javascript:`` links
* runtime exceptional states that can be switched on at will (see ``config.Faults``)
* a second "tenant" variant of the same product (see ``config.VARIANTS``)

All data is fake. Nothing here talks to any real system.
"""

from mockcore.app import create_app
from mockcore.config import Faults, MockCoreConfig

__all__ = ["Faults", "MockCoreConfig", "create_app"]
