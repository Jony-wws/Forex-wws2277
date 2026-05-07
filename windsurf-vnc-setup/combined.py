"""
All-in-one server:
- Static files for noVNC (combined.html, core/, vendor/, etc.)
- WebSocket /websockify proxying to local VNC on 127.0.0.1:5901
- POST /type and /key — text/key injection via xdotool
"""
import asyncio
import json
import socket
import subprocess

from aiohttp import web, WSMsgType, WSCloseCode

NOVNC_DIR = "/home/ubuntu/novnc-master"
VNC_HOST = "127.0.0.1"
VNC_PORT = 5901

DISPLAY_ENV = {
    "DISPLAY": ":0",
    "PATH": "/opt/.devin/package/custom_binaries:/usr/local/bin:/usr/bin:/bin",
    "HOME": "/home/ubuntu",
    "XAUTHORITY": "/home/ubuntu/.Xauthority",
    "LANG": "C.utf8",
    "LC_ALL": "C.utf8",
}


def find_windsurf_wid():
    try:
        out = subprocess.check_output(
            ["wmctrl", "-l"], env=DISPLAY_ENV, timeout=3
        ).decode()
    except Exception as e:
        print("wmctrl failed:", e, flush=True)
        return None
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 4 and parts[3].strip() == "Windsurf":
            return parts[0]
    return None


async def root_handler(request: web.Request) -> web.StreamResponse:
    raise web.HTTPFound("/combined.html")


async def type_handler(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    text = data.get("text", "")
    enter = bool(data.get("enter", False))
    focus_chat = bool(data.get("focus_chat", True))
    wid = find_windsurf_wid()
    if not wid:
        return web.json_response({"ok": False, "error": "Windsurf window not found"}, status=500)
    try:
        await asyncio.to_thread(
            subprocess.check_call,
            ["xdotool", "windowactivate", "--sync", wid],
            env=DISPLAY_ENV, timeout=5,
        )
        if focus_chat:
            # Click directly on Cascade chat input at relative position
            # (~75% width, ~92% height of Windsurf window).
            try:
                geo = subprocess.check_output(
                    ["xdotool", "getactivewindow", "getwindowgeometry", "--shell"],
                    env=DISPLAY_ENV, timeout=5,
                ).decode()
                gw = gh = None
                for line in geo.splitlines():
                    if line.startswith("WIDTH="):
                        gw = int(line.split("=", 1)[1])
                    elif line.startswith("HEIGHT="):
                        gh = int(line.split("=", 1)[1])
                if gw and gh:
                    cx = int(gw * 0.75)
                    cy = int(gh * 0.92)
                    await asyncio.to_thread(
                        subprocess.check_call,
                        ["xdotool", "mousemove", str(cx), str(cy), "click", "1"],
                        env=DISPLAY_ENV, timeout=5,
                    )
                    await asyncio.sleep(0.3)
            except Exception as e:
                print("focus_chat click failed:", e, flush=True)
        if text:
            await asyncio.to_thread(
                subprocess.check_call,
                ["xdotool", "type", "--delay", "6", "--", text],
                env=DISPLAY_ENV, timeout=60,
            )
        if enter:
            await asyncio.to_thread(
                subprocess.check_call,
                ["xdotool", "key", "Return"],
                env=DISPLAY_ENV, timeout=5,
            )
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def key_handler(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    k = data.get("key", "")
    if not k:
        return web.json_response({"ok": False, "error": "empty key"}, status=400)
    wid = find_windsurf_wid()
    if not wid:
        return web.json_response({"ok": False, "error": "Windsurf window not found"}, status=500)
    try:
        await asyncio.to_thread(
            subprocess.check_call,
            ["xdotool", "windowactivate", "--sync", wid],
            env=DISPLAY_ENV, timeout=5,
        )
        await asyncio.to_thread(
            subprocess.check_call,
            ["xdotool", "key", "--", k],
            env=DISPLAY_ENV, timeout=5,
        )
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def websockify_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(protocols=["binary"])
    await ws.prepare(request)
    loop = asyncio.get_running_loop()
    try:
        reader, writer = await asyncio.open_connection(VNC_HOST, VNC_PORT)
    except Exception as e:
        await ws.close(code=WSCloseCode.GOING_AWAY, message=f"vnc connect failed: {e}".encode())
        return ws

    async def ws_to_tcp():
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    writer.write(msg.data)
                    await writer.drain()
                elif msg.type == WSMsgType.TEXT:
                    writer.write(msg.data.encode())
                    await writer.drain()
                elif msg.type == WSMsgType.CLOSED or msg.type == WSMsgType.ERROR:
                    break
        except Exception as e:
            print("ws->tcp error:", e, flush=True)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def tcp_to_ws():
        try:
            while not ws.closed:
                data = await reader.read(65536)
                if not data:
                    break
                await ws.send_bytes(data)
        except Exception as e:
            print("tcp->ws error:", e, flush=True)
        finally:
            try:
                if not ws.closed:
                    await ws.close()
            except Exception:
                pass

    await asyncio.gather(ws_to_tcp(), tcp_to_ws(), return_exceptions=True)
    return ws


async def cors_middleware(app, handler):
    async def middleware_handler(request):
        if request.method == "OPTIONS":
            resp = web.Response(status=204)
        else:
            print(f"[req] {request.method} {request.path}", flush=True)
            resp = await handler(request)
        origin = request.headers.get("Origin", "*")
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp
    return middleware_handler


def make_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", root_handler)
    app.router.add_post("/type", type_handler)
    app.router.add_post("/key", key_handler)
    app.router.add_route("OPTIONS", "/type", lambda r: web.Response(status=204))
    app.router.add_route("OPTIONS", "/key", lambda r: web.Response(status=204))
    app.router.add_get("/websockify", websockify_handler)
    app.router.add_static("/", NOVNC_DIR, show_index=False)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=5050)
