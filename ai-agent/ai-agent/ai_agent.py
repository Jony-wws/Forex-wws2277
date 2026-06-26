#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# УНИВЕРСАЛЬНЫЙ ИИ-АГЕНТ (Groq) v2 — ОПТИМИЗИРОВАН ПРОТИВ ВЫЛЕТОВ
# Главные фиксы: PhotoImage только в главном потоке, глобальный перехват ошибок,
# защита всех потоков try/except, ограничение памяти. Агент больше НЕ вылетает.

import os, io, sys, json, time, threading, base64, re, datetime, traceback
import tkinter as tk
from tkinter import scrolledtext

# --- авто-установка зависимостей (если чего-то нет) ---
def _ensure(pkg, imp=None):
    try:
        __import__(imp or pkg)
    except ImportError:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", pkg], check=False)
for _p,_i in [("requests","requests"),("pyautogui","pyautogui"),("pillow","PIL")]:
    _ensure(_p,_i)

import requests
import pyautogui
from PIL import Image, ImageTk

try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

GROQ_KEY = "ВСТАВЬ_СЮДА_СВОЙ_GROQ_КЛЮЧ"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
HOME = os.path.expanduser("~")
MEM_FILE = os.path.join(HOME, "agent_memory.json")
REPORT_DIR = os.path.join(HOME, "agent_reports")
os.makedirs(REPORT_DIR, exist_ok=True)

pyautogui.FAILSAFE = False
SCREEN_W, SCREEN_H = pyautogui.size()

def load_memory():
    if os.path.exists(MEM_FILE):
        try:
            with open(MEM_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except Exception: pass
    return {"facts": [], "tasks": [], "chat": []}

def save_memory(mem):
    try:
        # ОГРАНИЧЕНИЕ памяти, чтобы файл не разрастался и не тормозил
        mem["chat"] = mem.get("chat", [])[-200:]
        mem["tasks"] = mem.get("tasks", [])[-100:]
        mem["facts"] = mem.get("facts", [])[-100:]
        with open(MEM_FILE, "w", encoding="utf-8") as f:
            json.dump(mem, f, ensure_ascii=False, indent=2)
    except Exception: pass

MEMORY = load_memory()

def mem_summary():
    facts = MEMORY.get("facts", [])[-15:]
    tasks = [t.get("task","") for t in MEMORY.get("tasks", [])[-8:]]
    s = ""
    if facts: s += "Факты о пользователе:\n- " + "\n- ".join(facts) + "\n"
    if tasks: s += "Недавние задачи:\n- " + "\n- ".join(tasks) + "\n"
    return s or "(память пустая)"

def grab():
    # Скриншот уменьшаем для скорости и надёжности (меньше шанс ошибки сети)
    img = pyautogui.screenshot()
    if img.width > 1280:
        nw = 1280; nh = int(img.height * nw / img.width)
        img = img.resize((nw, nh))
    buf = io.BytesIO(); img.save(buf, format="JPEG", quality=50)
    return img, base64.b64encode(buf.getvalue()).decode()

def groq_chat(messages, max_tokens=700, temp=0.4):
    payload = {"model": MODEL, "messages": messages, "temperature": temp, "max_tokens": max_tokens}
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    last = None
    for _ in range(4):
        try:
            r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=60)
            if r.status_code == 429: time.sleep(8); continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            last = e; time.sleep(4)
    raise last

CLASSIFY_SYS = ("Ты дружелюбный русскоязычный ассистент, управляющий компьютером. Определи: сообщение требует "
    "действий на ПК (открыть сайт/программу, кликать, искать, анализировать графики, собирать данные) ИЛИ это "
    "обычный разговор. Если нужны действия — ответь СТРОГО начиная с 'ЗАДАЧА:' и далее одним предложением "
    "переформулируй задачу по-русски. Иначе ответь обычным текстом по-русски.")

AGENT_SYS = f"""Ты УНИВЕРСАЛЬНЫЙ автономный агент на Windows-ПК ({SCREEN_W}x{SCREEN_H}). Управляешь мышью/клавиатурой как человек, собираешь данные, делаешь отчёты.

Каждый ход получаешь СКРИНШОТ. Отвечай ОДНИМ действием СТРОГИМ JSON (без markdown):
{{"thought":"что вижу и делаю (по-русски)","action":"ИМЯ","args":{{...}}}}

Действия:
- click/double_click/right_click {{"x":INT,"y":INT}}
- type {{"text":"..."}}
- press {{"keys":"enter"}}  (комбо "ctrl+c")
- scroll {{"amount":-600}}  (минус=вниз)
- open_app {{"app":"notepad"}}
- goto_url {{"url":"https://..."}}
- search {{"query":"..."}}  (Google)
- note {{"text":"найденные данные"}}  -> СОХРАНИТЬ в память (используй часто!)
- report {{"title":"...","content":"подробный отчёт/прогноз markdown по-русски"}}
- wait {{"seconds":2}}
- done {{"message":"итог"}}

ВАЖНО: координаты даны для изображения шириной 1280 — НО реальный экран {SCREEN_W}x{SCREEN_H}, координаты будут пересчитаны автоматически, указывай как видишь на картинке.
ПРАВИЛА: работай пошагово, проверяй результат на новом скриншоте, при ошибке пробуй иначе. Для анализа заходи на НЕСКОЛЬКО источников, делай "note" на каждом факте, в конце "report" затем "done". Только JSON, всё по-русски."""

def classify(message):
    msgs = [{"role":"system","content":CLASSIFY_SYS}]
    if MEMORY.get("facts"): msgs.append({"role":"system","content":"Память: "+mem_summary()})
    msgs.append({"role":"user","content":message})
    return groq_chat(msgs, max_tokens=300, temp=0.5).strip()

def agent_step(task, history, b64):
    content = [{"type":"text","text": f"ЗАДАЧА: {task}\n\nПамять:\n{mem_summary()}\n\nПоследние действия:\n" + ("\n".join(history[-8:]) if history else "(нет)") + "\n\nОтветь ОДНИМ JSON-действием."},
               {"type":"image_url","image_url":{"url": f"data:image/jpeg;base64,{b64}"}}]
    return groq_chat([{"role":"system","content":AGENT_SYS},{"role":"user","content":content}], max_tokens=600, temp=0.3)

def parse_json(text):
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if not m: return None
    raw = m.group(0)
    try: return json.loads(raw)
    except Exception:
        try: return json.loads(raw.replace("'", '"'))
        except Exception: return None

# Пересчёт координат с картинки 1280 на реальный экран
def _scale(v, axis):
    return v  # координаты pyautogui уже в реальных пикселях если экран<=1280; иначе масштаб
def do_action(act, scale_x=1.0, scale_y=1.0):
    a = act.get("action"); g = act.get("args",{}) or {}
    def X(): return int(g.get("x",0)*scale_x)
    def Y(): return int(g.get("y",0)*scale_y)
    if a == "click": pyautogui.click(X(), Y())
    elif a == "double_click": pyautogui.doubleClick(X(), Y())
    elif a == "right_click": pyautogui.rightClick(X(), Y())
    elif a == "type": pyautogui.write(g.get("text",""), interval=0.02)
    elif a == "press":
        k = g.get("keys","enter")
        pyautogui.hotkey(*[x.strip() for x in k.split("+")]) if "+" in k else pyautogui.press(k)
    elif a == "scroll": pyautogui.scroll(int(g.get("amount",-600)))
    elif a == "open_app":
        pyautogui.hotkey("win","r"); time.sleep(0.7); pyautogui.write(g.get("app","notepad"),interval=0.02); time.sleep(0.2); pyautogui.press("enter"); time.sleep(2)
    elif a == "goto_url": os.startfile(g.get("url","https://google.com")); time.sleep(3)
    elif a == "search":
        import urllib.parse
        os.startfile("https://www.google.com/search?q="+urllib.parse.quote(g.get("query",""))); time.sleep(3)
    elif a == "wait": time.sleep(float(g.get("seconds",1)))

BG="#0f1117"; CARD="#171a23"; ACCENT="#3b82f6"; GREEN="#22c55e"; RED="#ef4444"; TXT="#e5e7eb"; MUTED="#94a3b8"

class AgentGUI:
    def __init__(self, root):
        self.root = root
        root.title("ИИ-АГЕНТ")
        root.geometry("620x800+12+8")
        root.configure(bg=BG)
        root.attributes("-topmost", True)
        # ГЛОБАЛЬНЫЙ ПЕРЕХВАТ ОШИБОК TKINTER — окно НЕ закроется при ошибке
        root.report_callback_exception = self._on_error
        self.pending=None; self.busy=False; self.stop_flag=False; self.collected=[]; self.vis_w=580
        # масштаб картинки(1280) -> реальный экран
        self.scale_x = SCREEN_W/1280.0 if SCREEN_W>1280 else 1.0
        self.scale_y = self.scale_x

        head = tk.Frame(root, bg=BG); head.pack(fill=tk.X, padx=12, pady=(8,2))
        tk.Label(head, text="🤖 ИИ-АГЕНТ", bg=BG, fg=ACCENT, font=("Segoe UI",22,"bold")).pack(side=tk.LEFT)
        tk.Button(head, text="⛔ СТОП", command=self.stop, bg=RED, fg="white",
                  font=("Segoe UI",13,"bold"), relief=tk.FLAT, padx=10).pack(side=tk.RIGHT)

        tk.Label(root, text="👁️ Что видит агент:", bg=BG, fg=MUTED, font=("Segoe UI",12,"bold")).pack(anchor="w", padx=12)
        self.vision = tk.Label(root, bg=CARD, text="(ожидание задачи...)", fg=MUTED, font=("Segoe UI",12), height=9)
        self.vision.pack(fill=tk.X, padx=12, pady=3)

        tk.Label(root, text="💬 Диалог:", bg=BG, fg=MUTED, font=("Segoe UI",12,"bold")).pack(anchor="w", padx=12, pady=(4,0))
        self.log = scrolledtext.ScrolledText(root, bg=CARD, fg=TXT, font=("Segoe UI",17), wrap=tk.WORD,
                                             relief=tk.FLAT, padx=10, pady=8)
        self.log.configure(state=tk.DISABLED); self.log.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)

        bottom = tk.Frame(root, bg=BG); bottom.pack(fill=tk.X, padx=12, pady=(2,10))
        self.entry = tk.Entry(bottom, font=("Segoe UI",18), bg=CARD, fg=TXT, insertbackground=TXT, relief=tk.FLAT)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=12, padx=(0,6))
        self.entry.bind("<Return>", lambda e: self.send())
        tk.Button(bottom, text="➤", command=self.send, bg=GREEN, fg="#06210f",
                  font=("Segoe UI",20,"bold"), relief=tk.FLAT, width=3).pack(side=tk.LEFT)

        self.msg("✅ Готов! Открываю сайты, анализирую графики, собираю данные, делаю отчёты и прогнозы. Всё запоминаю.")
        self.msg("Примеры: 'проанализируй EUR/USD' • 'новости по биткоину + прогноз' • 'открой youtube'")
        if MEMORY.get("tasks"):
            self.msg(f"🧠 В памяти: {len(MEMORY.get('tasks',[]))} задач.")

    def _on_error(self, exc, val, tb):
        # Любая ошибка Tkinter попадает сюда — пишем в чат, НЕ закрываем окно
        err = "".join(traceback.format_exception(exc, val, tb))[-300:]
        self.msg(f"⚠️ Перехвачена ошибка (окно работает дальше):\n{err}")

    def msg(self, t):
        def _():
            try:
                self.log.configure(state=tk.NORMAL); self.log.insert(tk.END, t+"\n"); self.log.see(tk.END)
                self.log.configure(state=tk.DISABLED)
            except Exception: pass
        self.root.after(0, _)

    def show_vision(self, pil_img):
        # ВАЖНЫЙ ФИКС: PhotoImage создаётся ТОЛЬКО в главном потоке (иначе вылет!)
        try:
            w=self.vis_w; h=int(pil_img.height*w/pil_img.width)
            im = pil_img.resize((w, h))
            def _set():
                try:
                    photo = ImageTk.PhotoImage(im)   # <-- теперь в главном потоке
                    self.vision.configure(image=photo, text=""); self.vision.image=photo
                except Exception: pass
            self.root.after(0, _set)
        except Exception: pass

    def stop(self): self.stop_flag=True; self.msg("⛔ Остановлено.")

    def send(self):
        if self.busy: self.msg("⏳ Подожди, я работаю..."); return
        t = self.entry.get().strip()
        if not t: return
        self.entry.delete(0, tk.END)
        self.msg(f"\n👤 ТЫ: {t}")
        MEMORY["chat"].append({"role":"user","text":t,"time":str(datetime.datetime.now())}); save_memory(MEMORY)
        threading.Thread(target=self._safe_dispatch, args=(t,), daemon=True).start()

    def _safe_dispatch(self, task):
        # Поток НИКОГДА не уронит приложение
        try: self._dispatch(task)
        except Exception as e:
            self.msg(f"⚠️ Ошибка обработки (не критично): {str(e)[:200]}"); self.busy=False

    def _dispatch(self, task):
        YES=("да","ага","ок","окей","ok","давай","да!","+","yes","y","go","start","начни","начинай",
             "выполни","делай","поехали","da","ha","davay","poehali","vipolni","delay","nachni","yep","sure","да.")
        if self.pending:
            if task.strip().lower() in YES:
                pt=self.pending; self.pending=None; self.msg("▶️ Выполняю..."); self._run(pt); return
            else:
                self.pending=None; self.msg("❌ Отменил. Обрабатываю как новое...")
        self.busy=True; self.msg("🤔 Думаю...")
        try: reply = classify(task)
        except Exception as e: self.msg(f"❌ Ошибка ИИ: {str(e)[:200]}"); self.busy=False; return
        self.busy=False
        if reply.upper().startswith("ЗАДАЧА") or reply.upper().startswith("ZADACHA"):
            plan = reply.split(":",1)[1].strip() if ":" in reply else task
            self.pending=task
            self.msg(f"⚙️ Это ЗАДАЧА. Понял так: {plan}")
            self.msg("❓ Выполнить? Напиши 'да'.")
        else:
            self.msg(f"🤖 {reply}")
            MEMORY["chat"].append({"role":"agent","text":reply,"time":str(datetime.datetime.now())}); save_memory(MEMORY)

    def _run(self, task):
        self.busy=True; self.stop_flag=False; self.collected=[]; history=[]; steps_log=[]
        try:
            for step in range(40):
                if self.stop_flag: break
                try:
                    pil, b64 = grab(); self.show_vision(pil)
                except Exception as e:
                    self.msg(f"⚠️ Скриншот не вышел: {str(e)[:100]}"); time.sleep(1); continue
                try: raw = agent_step(task, history, b64)
                except Exception as e: self.msg(f"❌ Ошибка ИИ: {str(e)[:180]}"); break
                act = parse_json(raw)
                if not act: self.msg(f"💬 {raw[:300]}"); break
                name=act.get("action",""); th=act.get("thought","")
                if th: self.msg(f"🧠 {th}")
                if name=="note":
                    d=act.get("args",{}).get("text",""); self.collected.append(d)
                    self.msg(f"📝 Данные: {d[:160]}"); history.append(f"note:{d[:60]}"); continue
                if name=="report":
                    self._save_report(act.get("args",{}).get("title","Отчёт"), act.get("args",{}).get("content",""))
                    history.append("report"); continue
                if name=="done":
                    self.msg(f"✅ ГОТОВО: {act.get('args',{}).get('message','')}")
                    if self.collected and not any('report' in h for h in history): self._auto_report(task)
                    break
                self.msg(f"⚡ {name} {act.get('args',{})}")
                steps_log.append(f"{name}")
                try: do_action(act, self.scale_x, self.scale_y)
                except Exception as e: self.msg(f"⚠️ {str(e)[:140]}")
                history.append(f"{name} {act.get('args',{})}")
                time.sleep(1.0)
            else: self.msg("⏹️ Лимит шагов (40).")
        except Exception as e:
            self.msg(f"⚠️ Сбой задачи (окно работает): {str(e)[:200]}")
        finally:
            try:
                MEMORY["tasks"].append({"task":task,"time":str(datetime.datetime.now()),"steps":steps_log,"data":self.collected})
                save_memory(MEMORY)
            except Exception: pass
            self.busy=False

    def _save_report(self, title, content):
        ts=datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe=re.sub(r'[^\w\- ]','',title)[:40] or "report"
        path=os.path.join(REPORT_DIR, f"{safe}_{ts}.md")
        try:
            with open(path,"w",encoding="utf-8") as f: f.write(f"# {title}\n\n{content}\n")
            self.msg(f"📑 Отчёт сохранён: {path}")
            self.msg(f"━━━ ОТЧЁТ ━━━\n{content[:1200]}\n━━━━━━━━━")
        except Exception as e: self.msg(f"⚠️ {e}")

    def _auto_report(self, task):
        try:
            data="\n".join(f"- {d}" for d in self.collected)
            rep=groq_chat([{"role":"user","content":f"Составь подробный итоговый отчёт с выводами и прогнозом по-русски (markdown) по задаче '{task}':\n{data}"}], max_tokens=900)
            self._save_report(f"Отчёт {task[:25]}", rep)
        except Exception: pass

if __name__ == "__main__":
    root = tk.Tk(); AgentGUI(root); root.mainloop()
