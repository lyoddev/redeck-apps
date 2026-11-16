#!/usr/bin/env python3
"""ReDeck Launcher: окно для установки и запуска программ из репо redeck-apps (работает поверх install.sh)."""
import glob
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

HOME = os.path.expanduser("~")
SELF = os.path.abspath(__file__)
ROOT = os.path.join(HOME, ".local/share/redeck/apps")
BIN = os.path.join(HOME, ".local/bin")
DESK = os.path.join(HOME, ".local/share/applications")
CACHE = os.path.join(HOME, ".cache/redeck-launcher")
CFG = os.path.join(HOME, ".config/redeck-launcher.txt")
LAUNCHER_ICON = os.path.join(os.path.dirname(SELF), "launcher-icon.svg")

BG, CARD, FG, DIM, ACC, RED = "#0b0f0c", "#111a14", "#c8ffd8", "#5f8f6f", "#00ff88", "#ff4d6d"
FONT = ("monospace", 10)
FONT_B = ("monospace", 12, "bold")

# Режим «спросить пароль»: его вызывает sudo внутри install.sh, пока лончер ставит пакеты.
if "--askpass" in sys.argv:
    r = tk.Tk()
    r.withdraw()
    pw = simpledialog.askstring("sudo", "Пароль (нужен для установки системных пакетов):", show="*", parent=r)
    if pw is None:
        sys.exit(1)
    print(pw)
    sys.exit(0)


def has_installer(d):
    return bool(d) and os.path.isfile(os.path.join(d, "install.sh"))


def find_repo():
    for d in (os.path.dirname(SELF), os.path.join(HOME, "apps/redeck-apps")):
        if has_installer(d):
            return d
    try:
        d = open(CFG, encoding="utf-8").read().strip()
        if has_installer(d):
            return d
    except OSError:
        pass
    d = filedialog.askdirectory(title="Где лежит репо redeck-apps (там, где install.sh)")
    if has_installer(d):
        os.makedirs(os.path.dirname(CFG), exist_ok=True)
        open(CFG, "w", encoding="utf-8").write(d)
        return d
    sys.exit("Не нашёл репо с install.sh")


def parse_conf(path):
    d = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        try:
            parts = shlex.split(v)
        except ValueError:
            parts = [v]
        d[k.strip()] = parts[0] if parts else ""
    return d


def load_apps(repo):
    apps = []
    for c in sorted(glob.glob(os.path.join(repo, "apps", "*", "app.conf"))):
        try:
            d = parse_conf(c)
        except OSError:
            continue
        d["_dir"] = os.path.dirname(c)
        d.setdefault("ID", os.path.basename(d["_dir"]))
        d.setdefault("NAME", d["ID"])
        apps.append(d)
    return apps


def is_installed(a):
    return os.path.isfile(os.path.join(BIN, a["ID"])) and os.path.isdir(os.path.join(ROOT, a["ID"]))


def needle(a):
    if a.get("RUN_MODULE"):
        return "-m " + a["RUN_MODULE"]
    return os.path.join(ROOT, a["ID"], a.get("MAIN", ""))


def is_running(a):
    return subprocess.run(["pgrep", "-f", "--", needle(a)], capture_output=True).returncode == 0


def svg_to_png(src, name, size):
    png = os.path.join(CACHE, "%s_%d.png" % (name, size))
    os.makedirs(CACHE, exist_ok=True)
    if not os.path.exists(png) or os.path.getmtime(png) < os.path.getmtime(src):
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        GdkPixbuf.Pixbuf.new_from_file_at_size(src, size, size).savev(png, "png", [], [])
    return png


def icon_for(a):
    src = os.path.join(a["_dir"], a.get("ICON_FILE", ""))
    if not a.get("ICON_FILE") or not os.path.isfile(src):
        return None
    try:
        return tk.PhotoImage(file=svg_to_png(src, a["ID"], 40))
    except Exception:
        return None


class Launcher:
    def __init__(self, root, repo):
        self.root, self.repo = root, repo
        self.busy = False
        self.rows = []
        self.images = []
        root.title("Re Launcher")
        root.configure(bg=BG)
        root.geometry("760x600")

        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=14, pady=(14, 6))
        self.win_icon = self.head_icon = None
        if os.path.isfile(LAUNCHER_ICON):
            try:
                self.win_icon = tk.PhotoImage(file=svg_to_png(LAUNCHER_ICON, "launcher", 64))
                root.iconphoto(True, self.win_icon)
                self.head_icon = tk.PhotoImage(file=svg_to_png(LAUNCHER_ICON, "launcher", 32))
                tk.Label(head, image=self.head_icon, bg=BG).pack(side="left", padx=(0, 10))
            except Exception:
                pass
        tk.Label(head, text="Re Launcher", bg=BG, fg=ACC, font=("monospace", 16, "bold")).pack(side="left")
        self.btn(head, "→ в меню", self.make_shortcut).pack(side="right")
        self.btn(head, "git pull", self.pull).pack(side="right", padx=6)
        self.btn(head, "↻", self.refresh).pack(side="right")
        self.busy_lbl = tk.Label(head, text="", bg=BG, fg=ACC, font=FONT)
        self.busy_lbl.pack(side="right", padx=8)

        self.list = tk.Frame(root, bg=BG)
        self.list.pack(fill="both", expand=True, padx=14)

        self.log = tk.Text(root, height=10, bg="#060907", fg=DIM, font=("monospace", 9),
                           relief="flat", state="disabled")
        self.log.pack(fill="x", padx=14, pady=14)

        self.refresh()
        self.tick()

    def btn(self, parent, text, cmd, color=FG):
        return tk.Button(parent, text=text, command=cmd, bg=CARD, fg=color, activebackground=ACC,
                         activeforeground=BG, relief="flat", font=FONT, padx=8, pady=3, bd=0)

    def write(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def refresh(self):
        for w in self.list.winfo_children():
            w.destroy()
        self.rows.clear()
        self.images.clear()
        for a in load_apps(self.repo):
            self.add_row(a)

    def add_row(self, a):
        installed = is_installed(a)
        row = tk.Frame(self.list, bg=CARD)
        row.pack(fill="x", pady=3)

        cell = tk.Frame(row, bg=CARD, width=48, height=48)
        cell.pack_propagate(False)
        cell.pack(side="left", padx=8, pady=6)
        icon = tk.Label(cell, text="▣", bg=CARD, fg=ACC if installed else DIM, font=("monospace", 22))
        img = icon_for(a)
        if img:
            self.images.append(img)
            icon.configure(image=img, text="")
        icon.pack(expand=True)

        info = tk.Frame(row, bg=CARD)
        info.pack(side="left", fill="x", expand=True)
        tk.Label(info, text=a["NAME"], bg=CARD, fg=FG if installed else DIM, font=FONT_B, anchor="w").pack(fill="x")
        tk.Label(info, text=a.get("COMMENT", ""), bg=CARD, fg=DIM, font=("monospace", 9), anchor="w").pack(fill="x")

        if not installed:
            self.btn(row, "Установить", lambda a=a: self.run(["bash", "install.sh", a["ID"]], "установка " + a["NAME"]), ACC).pack(side="right", padx=8)
            return

        self.btn(row, "✕", lambda a=a: self.remove(a), RED).pack(side="right", padx=(2, 8))
        self.btn(row, "Ярлык", lambda a=a: self.run(["bash", "install.sh", "ярлык", a["ID"]], "ярлык " + a["NAME"])).pack(side="right", padx=2)
        self.btn(row, "Обновить", lambda a=a: self.update(a)).pack(side="right", padx=2)
        run_btn = self.btn(row, "Запуск", lambda a=a: self.toggle(a), ACC)
        run_btn.pack(side="right", padx=2)
        dot = tk.Label(row, text="●", bg=CARD, fg=DIM, font=FONT)
        dot.pack(side="right", padx=4)
        self.rows.append((a, dot, run_btn))

    def tick(self):
        for a, dot, run_btn in self.rows:
            running = is_running(a)
            dot.configure(fg=ACC if running else DIM)
            run_btn.configure(text="Стоп" if running else "Запуск", fg=RED if running else ACC)
        self.root.after(3000, self.tick)

    def toggle(self, a):
        if is_running(a):
            subprocess.run(["pkill", "-f", "--", needle(a)])
            self.write("стоп: " + a["NAME"])
        else:
            try:
                subprocess.Popen([os.path.join(BIN, a["ID"])], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.write("запуск: " + a["NAME"])
            except OSError as e:
                self.write("не запустилось: %s" % e)

    def update(self, a):
        """Быстрое обновление: перекопировать файлы из репо. Полная переустановка только если поменялись библиотеки."""
        if self.busy:
            self.write("подожди, предыдущая команда ещё идёт")
            return
        src, dst = a["_dir"], os.path.join(ROOT, a["ID"])
        keys = ("PIP", "APT", "PYMODE", "SYSTEM_SITE", "USE_ENV_OF", "RUN_MODULE", "MAIN")

        def deps(d):
            try:
                conf = parse_conf(os.path.join(d, "app.conf"))
                req_path = os.path.join(d, "requirements.txt")
                req = open(req_path, encoding="utf-8").read() if os.path.exists(req_path) else ""
                return [conf.get(k, "") for k in keys], req
            except OSError:
                return None

        if deps(src) != deps(dst):
            self.run(["bash", "install.sh", a["ID"]], "полное обновление %s (поменялись библиотеки)" % a["NAME"])
            return
        try:
            shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("venv", "__pycache__"))
        except OSError as e:
            self.write("✘ не скопировалось: %s" % e)
            return
        self.run(["bash", "install.sh", "ярлык", a["ID"]], "обновление " + a["NAME"] + " (перезапусти программу, если она открыта)")

    def remove(self, a):
        if messagebox.askyesno("Удалить", "Удалить %s?" % a["NAME"]):
            self.run(["bash", "install.sh", "удалить", a["ID"]], "удаление " + a["NAME"])

    def pull(self):
        self.run(["git", "pull"], "git pull")

    def make_shortcut(self):
        os.makedirs(DESK, exist_ok=True)
        icon = "applications-other"
        hicolor = os.path.join(HOME, ".local/share/icons/hicolor")
        if os.path.isfile(LAUNCHER_ICON):
            d = os.path.join(hicolor, "scalable/apps")
            os.makedirs(d, exist_ok=True)
            shutil.copy(LAUNCHER_ICON, os.path.join(d, "io.redeck.ReLauncher.svg"))
            icon = "io.redeck.ReLauncher"
        old = os.path.join(DESK, "io.redeck.Launcher.desktop")
        if os.path.exists(old):
            os.remove(old)
        path = os.path.join(DESK, "io.redeck.ReLauncher.desktop")
        with open(path, "w", encoding="utf-8") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Re Launcher\n"
                    "Comment=Установка и запуск программ ReDeck\n"
                    'Exec=python3 "%s"\nIcon=%s\nTerminal=false\nCategories=Utility;\n'
                    "StartupWMClass=ReLauncher\n" % (SELF, icon))
        subprocess.run(["gtk-update-icon-cache", "-f", "-t", hicolor], capture_output=True)
        subprocess.run(["update-desktop-database", DESK], capture_output=True)
        self.write("ярлык лончера добавлен в меню приложений")

    def run(self, cmd, label):
        if self.busy:
            self.write("подожди, предыдущая команда ещё идёт")
            return
        self.busy = True
        self.busy_lbl.configure(text="⏳ идёт команда…")
        self.write("▶ " + label)
        threading.Thread(target=self._run, args=(cmd,), daemon=True).start()

    def _run(self, cmd):
        tmp = tempfile.mkdtemp()
        try:
            # sudo внутри install.sh спросит пароль через окошко, а не через терминал
            for name, body in (("sudo", '#!/bin/sh\nexec /usr/bin/sudo -A "$@"\n'),
                               ("askpass", '#!/bin/sh\nexec python3 "%s" --askpass\n' % SELF)):
                p = os.path.join(tmp, name)
                open(p, "w").write(body)
                os.chmod(p, 0o755)
            env = dict(os.environ, PATH=tmp + ":" + os.environ.get("PATH", ""),
                       SUDO_ASKPASS=os.path.join(tmp, "askpass"))
            proc = subprocess.Popen(cmd, cwd=self.repo, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                self.root.after(0, self.write, line.rstrip())
            proc.wait()
            msg = "✔ готово" if proc.returncode == 0 else "✘ ошибка, код %d" % proc.returncode
        except Exception as e:
            msg = "✘ ошибка: %s" % e
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.root.after(0, self.done, msg)

    def done(self, msg):
        self.write(msg)
        self.busy = False
        self.busy_lbl.configure(text="")
        self.refresh()


if __name__ == "__main__":
    root = tk.Tk(className="ReLauncher")
    Launcher(root, find_repo())
    root.mainloop()
