"""The HTTPS edge's control (K blueprint T2; decisions L42, L43, D44 refined).

Run as ``python -m app.edge_control`` by the ``edge-control`` service
alone: the backend's image in a container of its own, with the edge's
control volume and the Caddyfile, and no database URL, no secret and no
agent. It holds Caddy's admin socket and the files a platform admin
uploads; the backend never imports this package and never mounts that
volume, so a key never enters the process an in-process agent runs in
(L42). :mod:`app.edge_control.edge` is what it does, and
:mod:`app.edge_control.__main__` how it answers.
"""
