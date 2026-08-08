"""ki-protocol — KubeIntellect SSE wire protocol, shared by server and clients.

Import the view you need:

    from ki_protocol import wire      # server emission models (flat wire shape)
    from ki_protocol import events    # client typed envelope + parse_event()

Both views describe protocol version `PROTOCOL_VERSION`; the wire module is
canonical for what the server sends, the events module is canonical for how
clients parse it. Class names overlap between the two views deliberately
(StatusEvent, TokenEvent, ...) — always import via the submodule.
"""

from ki_protocol.wire import PROTOCOL_VERSION

__all__ = ["PROTOCOL_VERSION"]
