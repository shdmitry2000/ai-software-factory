"""Who is on the other end of the console's unix socket.

sshd connects a forwarded unix socket (`ssh -L 8080:/run/factory-console/sock`) as the
signed-in OS Login user, so the kernel knows the peer's uid (SO_PEERCRED). The console
lets in root and members of the `factory-operators` group; everyone else gets 403.

`PeerCredH11` is uvicorn's h11 protocol with that uid put into every request's ASGI
scope as `scope["peercred"] = (pid, uid, gid)`.
"""
from __future__ import annotations

import grp
import os
import pwd
import socket
import struct
import sys
from dataclasses import dataclass, field

from uvicorn.protocols.http.h11_impl import H11Protocol

GROUP = "factory-operators"


def peer_of(sock: socket.socket | None) -> tuple[int, int, int] | None:
    if sock is None or sock.family != socket.AF_UNIX or not sys.platform.startswith("linux"):
        return None
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)


@dataclass(frozen=True)
class Operator:
    name: str
    uid: int | None = None
    allowed: bool = False
    how: str = "peercred"                 # peercred | dev
    groups: tuple[str, ...] = field(default_factory=tuple)


def operator_for(uid: int, group: str = GROUP) -> Operator:
    try:
        pw = pwd.getpwuid(uid)
        name, gid = pw.pw_name, pw.pw_gid
    except KeyError:
        return Operator(f"uid-{uid}", uid, uid == 0)
    try:
        gids = os.getgrouplist(name, gid)          # all sources: files and OS Login
    except OSError:
        gids = [gid]
    names = []
    for g in gids:
        try:
            names.append(grp.getgrgid(g).gr_name)
        except KeyError:
            pass
    try:                                           # members listed in /etc/group
        if name in grp.getgrnam(group).gr_mem and group not in names:
            names.append(group)
    except KeyError:
        pass
    return Operator(name, uid, uid == 0 or group in names, "peercred", tuple(sorted(set(names))))


class PeerCredH11(H11Protocol):
    def connection_made(self, transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        creds = peer_of(transport.get_extra_info("socket"))
        inner = self.app

        async def app(scope, receive, send):
            if scope.get("type") in ("http", "websocket"):
                scope = {**scope, "peercred": creds}
            await inner(scope, receive, send)

        self.app = app
