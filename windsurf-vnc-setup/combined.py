"""
All-in-one server for "Phone-as-Computer-Keyboard" mode.

Endpoints:
- GET  /                       redirect to /combined.html
- GET  /combined.html          UI (served from NOVNC_DIR after setup.sh
                               copies it there); also served directly
                               from this directory as fallback.
- GET  /websockify             WebSocket -> TCP proxy to local TigerVNC
                               on 127.0.0.1:5901
- POST /type                   {text, enter, focus_chat?}        — type text
- POST /key                    {key, target?}                    — single key / chord
                               target: "windsurf" (legacy) | "active" (default)
- POST /click                  {button?=1, double?=false, x?, y?, modifiers?}
- POST /mousemove              {x, y}
- POST /drag                   {x1, y1, x2, y2, button?=1}
- POST /scroll                 {direction: up|down|left|right, amount?=3}
- POST /clipboard_set          {text}                            — set X clipboard
- GET  /clipboard_get                                            — read X clipboard
- POST /clipboard_copy_active                                    — Ctrl+C in active window
                                                                   then return X clipboard
- POST /openurl                {url}                             — xdg-open the url
- POST /focus_windsurf                                           — activate Windsurf
- POST /focus_chat                                               — click cascade input
- GET  /screen_info                                              — returns active window
                                                                   geometry & screen size
"""
import asyncio
import json
import os
import socket
import subprocess
from pathlib import Path

from aiohttp import web, WSMsgType, WSCloseCode

NOVNC_DIR = "/home/ubuntu/novnc-master"
HERE_DIR = str(Path(__file__).resolve().parent)
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


# ---------- helpers ------------------------------------------------------

def run(cmd, timeout=10, input_bytes=None, check=True):
    """Run a subprocess with DISPLAY_ENV. Returns CompletedProcess."""
    return subprocess.run(
        cmd,
        env=DISPLAY_ENV,
        timeout=timeout,
        input=input_bytes,
        capture_output=True,
        check=check,
    )


async def arun(cmd, timeout=10, input_bytes=None, check=True):
    return await asyncio.to_thread(run, cmd, timeout, input_bytes, check)


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


async def activate_windsurf_if_present():
    wid = find_windsurf_wid()
    if not wid:
        return None
    try:
        await arun(["xdotool", "windowactivate", "--sync", wid], timeout=5)
    except Exception as e:
        print("activate windsurf failed:", e, flush=True)
        return None
    return wid


def json_err(msg, status=500):
    return web.json_response({"ok": False, "error": str(msg)}, status=status)


# ---------- handlers -----------------------------------------------------

async def root_handler(request: web.Request) -> web.StreamResponse:
    raise web.HTTPFound("/combined.html")


async def combined_html_handler(request: web.Request) -> web.StreamResponse:
    """Serve combined.html either from NOVNC_DIR (preferred, has noVNC core
    siblings) or from the source directory (fallback for dev runs)."""
    candidates = [
        os.path.join(NOVNC_DIR, "combined.html"),
        os.path.join(HERE_DIR, "combined.html"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return web.FileResponse(path)
    return web.Response(status=404, text="combined.html not found")


async def type_handler(request: web.Request) -> web.Response:
    """Type text into currently focused window (or Windsurf if focus_chat)."""
    try:
        data = await request.json()
    except Exception:
        return json_err("bad json", 400)
    text = data.get("text", "")
    enter = bool(data.get("enter", False))
    focus_chat = bool(data.get("focus_chat", False))

    try:
        if focus_chat:
            wid = await activate_windsurf_if_present()
            if wid:
                # Click the Cascade chat input (~75% × 92% of Windsurf window).
                try:
                    geo = (await arun(
                        ["xdotool", "getactivewindow",
                         "getwindowgeometry", "--shell"],
                        timeout=5,
                    )).stdout.decode()
                    gw = gh = None
                    for line in geo.splitlines():
                        if line.startswith("WIDTH="):
                            gw = int(line.split("=", 1)[1])
                        elif line.startswith("HEIGHT="):
                            gh = int(line.split("=", 1)[1])
                    if gw and gh:
                        cx = int(gw * 0.75)
                        cy = int(gh * 0.92)
                        await arun(
                            ["xdotool", "mousemove", str(cx), str(cy),
                             "click", "1"],
                            timeout=5,
                        )
                        await asyncio.sleep(0.3)
                except Exception as e:
                    print("focus_chat click failed:", e, flush=True)
        if text:
            await arun(
                ["xdotool", "type", "--delay", "6", "--", text],
                timeout=120,
            )
        if enter:
            await arun(["xdotool", "key", "Return"], timeout=5)
        return web.json_response({"ok": True})
    except Exception as e:
        return json_err(e)


async def key_handler(request: web.Request) -> web.Response:
    """Send a single key / chord (e.g. "Return", "ctrl+c", "alt+F4")."""
    try:
        data = await request.json()
    except Exception:
        return json_err("bad json", 400)
    k = data.get("key", "")
    target = (data.get("target") or "active").lower()
    if not k:
        return json_err("empty key", 400)
    try:
        if target == "windsurf":
            await activate_windsurf_if_present()
        await arun(["xdotool", "key", "--", k], timeout=5)
        return web.json_response({"ok": True})
    except Exception as e:
        return json_err(e)


async def click_handler(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return json_err("bad json", 400)
    button = int(data.get("button", 1))
    double = bool(data.get("double", False))
    x = data.get("x")
    y = data.get("y")
    modifiers = data.get("modifiers") or []  # e.g. ["ctrl", "shift"]
    try:
        if x is not None and y is not None:
            await arun(["xdotool", "mousemove", str(int(x)), str(int(y))],
                       timeout=5)
        # Apply modifiers via keydown ... click ... keyup
        for m in modifiers:
            await arun(["xdotool", "keydown", m], timeout=3)
        try:
            if double:
                await arun(["xdotool", "click", "--repeat", "2",
                            "--delay", "60", str(button)], timeout=5)
            else:
                await arun(["xdotool", "click", str(button)], timeout=5)
        finally:
            for m in reversed(modifiers):
                try:
                    await arun(["xdotool", "keyup", m], timeout=3)
                except Exception:
                    pass
        return web.json_response({"ok": True})
    except Exception as e:
        return json_err(e)


async def mousemove_handler(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return json_err("bad json", 400)
    try:
        x = int(data["x"])
        y = int(data["y"])
    except Exception:
        return json_err("x and y required", 400)
    try:
        await arun(["xdotool", "mousemove", str(x), str(y)], timeout=5)
        return web.json_response({"ok": True})
    except Exception as e:
        return json_err(e)


async def drag_handler(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return json_err("bad json", 400)
    try:
        x1 = int(data["x1"]); y1 = int(data["y1"])
        x2 = int(data["x2"]); y2 = int(data["y2"])
    except Exception:
        return json_err("x1,y1,x2,y2 required", 400)
    button = int(data.get("button", 1))
    try:
        await arun(["xdotool", "mousemove", str(x1), str(y1)], timeout=5)
        await arun(["xdotool", "mousedown", str(button)], timeout=5)
        await asyncio.sleep(0.05)
        await arun(["xdotool", "mousemove", str(x2), str(y2)], timeout=5)
        await asyncio.sleep(0.05)
        await arun(["xdotool", "mouseup", str(button)], timeout=5)
        return web.json_response({"ok": True})
    except Exception as e:
        return json_err(e)


_SCROLL_BTN = {"up": 4, "down": 5, "left": 6, "right": 7}


async def scroll_handler(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return json_err("bad json", 400)
    direction = (data.get("direction") or "down").lower()
    amount = int(data.get("amount", 3))
    btn = _SCROLL_BTN.get(direction)
    if btn is None:
        return json_err("direction must be up|down|left|right", 400)
    try:
        await arun(
            ["xdotool", "click", "--repeat", str(max(1, amount)),
             "--delay", "30", str(btn)],
            timeout=10,
        )
        return web.json_response({"ok": True})
    except Exception as e:
        return json_err(e)


# ---------- clipboard ----------------------------------------------------

def _xclip_get(selection="clipboard"):
    p = subprocess.run(
        ["xclip", "-selection", selection, "-o"],
        env=DISPLAY_ENV, timeout=5, capture_output=True,
    )
    if p.returncode != 0:
        # xclip returns 1 when clipboard is empty — treat as empty string
        return ""
    try:
        return p.stdout.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _xclip_set(text, selection="clipboard"):
    subprocess.run(
        ["xclip", "-selection", selection, "-i"],
        env=DISPLAY_ENV, timeout=5, input=text.encode("utf-8"), check=True,
    )
    # Also set primary so middle-click paste works
    if selection == "clipboard":
        try:
            subprocess.run(
                ["xclip", "-selection", "primary", "-i"],
                env=DISPLAY_ENV, timeout=5, input=text.encode("utf-8"),
                check=False,
            )
        except Exception:
            pass


async def clipboard_get_handler(request: web.Request) -> web.Response:
    try:
        text = await asyncio.to_thread(_xclip_get, "clipboard")
        return web.json_response({"ok": True, "text": text})
    except Exception as e:
        return json_err(e)


async def clipboard_set_handler(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return json_err("bad json", 400)
    text = data.get("text", "")
    try:
        await asyncio.to_thread(_xclip_set, text, "clipboard")
        return web.json_response({"ok": True})
    except Exception as e:
        return json_err(e)


async def clipboard_copy_active_handler(request: web.Request) -> web.Response:
    """Send Ctrl+C to the active window, wait briefly, then read X clipboard."""
    try:
        await arun(["xdotool", "key", "ctrl+c"], timeout=5)
        await asyncio.sleep(0.15)
        text = await asyncio.to_thread(_xclip_get, "clipboard")
        return web.json_response({"ok": True, "text": text})
    except Exception as e:
        return json_err(e)


# ---------- open url -----------------------------------------------------

async def openurl_handler(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return json_err("bad json", 400)
    url = (data.get("url") or "").strip()
    if not url:
        return json_err("url required", 400)
    if not (url.startswith("http://") or url.startswith("https://")
            or url.startswith("file://")):
        url = "https://" + url
    try:
        # Detached so the response doesn't wait on the browser.
        subprocess.Popen(
            ["xdg-open", url],
            env=DISPLAY_ENV,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return web.json_response({"ok": True, "url": url})
    except Exception as e:
        return json_err(e)


# ---------- windsurf focus / screen info --------------------------------

async def focus_windsurf_handler(request: web.Request) -> web.Response:
    wid = await activate_windsurf_if_present()
    if not wid:
        return json_err("Windsurf window not found")
    return web.json_response({"ok": True, "wid": wid})


async def focus_chat_handler(request: web.Request) -> web.Response:
    try:
        wid = await activate_windsurf_if_present()
        if not wid:
            return json_err("Windsurf window not found")
        geo = (await arun(
            ["xdotool", "getactivewindow", "getwindowgeometry", "--shell"],
            timeout=5,
        )).stdout.decode()
        gw = gh = None
        for line in geo.splitlines():
            if line.startswith("WIDTH="):
                gw = int(line.split("=", 1)[1])
            elif line.startswith("HEIGHT="):
                gh = int(line.split("=", 1)[1])
        if not (gw and gh):
            return json_err("could not read geometry")
        cx = int(gw * 0.75); cy = int(gh * 0.92)
        await arun(["xdotool", "mousemove", str(cx), str(cy),
                    "click", "1"], timeout=5)
        return web.json_response({"ok": True})
    except Exception as e:
        return json_err(e)


async def screen_info_handler(request: web.Request) -> web.Response:
    info = {"ok": True}
    try:
        out = (await arun(
            ["xdotool", "getdisplaygeometry"], timeout=3, check=False,
        )).stdout.decode().strip().split()
        if len(out) == 2:
            info["screen_width"] = int(out[0])
            info["screen_height"] = int(out[1])
    except Exception as e:
        info["screen_error"] = str(e)
    try:
        out = (await arun(
            ["xdotool", "getactivewindow", "getwindowgeometry", "--shell"],
            timeout=3, check=False,
        )).stdout.decode()
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info["active_" + k.lower()] = v
    except Exception as e:
        info["active_error"] = str(e)
    return web.json_response(info)


# ---------- websockify proxy --------------------------------------------

async def websockify_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(protocols=["binary"])
    await ws.prepare(request)
    try:
        reader, writer = await asyncio.open_connection(VNC_HOST, VNC_PORT)
    except Exception as e:
        await ws.close(code=WSCloseCode.GOING_AWAY,
                       message=f"vnc connect failed: {e}".encode())
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
                elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
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


# ---------- middleware / app --------------------------------------------

async def cors_middleware(app, handler):
    async def middleware_handler(request):
        if request.method == "OPTIONS":
            resp = web.Response(status=204)
        else:
            print(f"[req] {request.method} {request.path}", flush=True)
            try:
                resp = await handler(request)
            except web.HTTPException as e:
                resp = e
        origin = request.headers.get("Origin", "*")
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp
    return middleware_handler


def make_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware],
                          client_max_size=4 * 1024 * 1024)
    app.router.add_get("/", root_handler)
    # Serve combined.html explicitly so it works even when NOVNC_DIR is empty.
    app.router.add_get("/combined.html", combined_html_handler)

    app.router.add_post("/type", type_handler)
    app.router.add_post("/key", key_handler)
    app.router.add_post("/click", click_handler)
    app.router.add_post("/mousemove", mousemove_handler)
    app.router.add_post("/drag", drag_handler)
    app.router.add_post("/scroll", scroll_handler)

    app.router.add_get("/clipboard_get", clipboard_get_handler)
    app.router.add_post("/clipboard_set", clipboard_set_handler)
    app.router.add_post("/clipboard_copy_active", clipboard_copy_active_handler)

    app.router.add_post("/openurl", openurl_handler)
    app.router.add_post("/focus_windsurf", focus_windsurf_handler)
    app.router.add_post("/focus_chat", focus_chat_handler)
    app.router.add_get("/screen_info", screen_info_handler)

    for p in ("/type", "/key", "/click", "/mousemove", "/drag", "/scroll",
              "/clipboard_set", "/clipboard_copy_active",
              "/openurl", "/focus_windsurf", "/focus_chat"):
        app.router.add_route("OPTIONS", p, lambda r: web.Response(status=204))

    app.router.add_get("/websockify", websockify_handler)
    # Static must be last so explicit routes take precedence.
    if os.path.isdir(NOVNC_DIR):
        app.router.add_static("/", NOVNC_DIR, show_index=False)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=5050)
