"""应用入口 — `uvicorn ansys_hip.main:app`(host/port 见 config/service.yaml).

等价直跑:python -m ansys_hip.main(读取 service.yaml 的 server.host/port)。
"""

from __future__ import annotations

from .api import create_app

app = create_app()


if __name__ == "__main__":
    import uvicorn

    from .settings import load_settings

    settings = load_settings()
    uvicorn.run("ansys_hip.main:app", host=settings.server.host, port=settings.server.port)
