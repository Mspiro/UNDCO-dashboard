#!/usr/bin/env python3
import json, os, re, signal, socket, subprocess, sys, threading, time, webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG = Path(__file__).resolve().with_name("config.json")  # remembers the project path and light/dark theme


def load_config():
    try:
        return json.loads(CONFIG.read_text())
    except Exception:
        return {}


def save_config(**kv):
    CONFIG.write_text(json.dumps({**load_config(), **kv}))


def is_project(p):
    return (p / ".ddev").is_dir() and (p / "web/themes/custom/undco_theme").is_dir()


def find_project():
    """Project path from: CLI arg > saved config.json > auto-detect (cwd/parents, folders next to this script) > ask."""
    here = Path(__file__).resolve().parent
    if len(sys.argv) > 1:
        tries = [Path(sys.argv[1]).expanduser()]
    else:
        try:
            tries = [Path(load_config()["project"])]
        except Exception:
            tries = []
        tries += [Path.cwd(), *Path.cwd().parents, *sorted(here.parent.iterdir())]
    for p in tries:
        if p.is_dir() and is_project(p.resolve()):
            break
    else:
        while True:
            p = Path(input("Project path (folder with .ddev and web/themes/custom/undco_theme): ").strip()).expanduser()
            if is_project(p.resolve()):
                break
            print(f"Not the project: {p}")
    p = p.resolve()
    save_config(project=str(p))
    return p


PROJECT = find_project()
THEMES = PROJECT / "web/themes/custom"
BASE = "undco_theme"  # base theme: shared by every site row, never its own row
PORT = 8800  # preferred; next free one is used if taken

# label, drush alias, theme dir, storybook port
ROWS = [
    ("UNDCO", "undco", BASE, 6006),
    ("UNCT", "unct", "unct", 6009),
    ("UNSDG", "unsdg", "sdg", 6008),
]
# Node from nvm when installed (theme .nvmrc = 24), else system node. Then install deps if node_modules is missing.
NVM = ('if [ -s ~/.nvm/nvm.sh ]; then source ~/.nvm/nvm.sh >/dev/null; nvm use >/dev/null 2>&1 || nvm install; '
       'else echo "nvm not found, using system node"; fi; echo "node $(node -v)"; ')
DEPS = ('{ [ -d node_modules ] || { echo "== node_modules missing in $(basename $PWD): running npm ci (first time only)"; '
        'npm ci; }; } && ')

procs, logs, uli = {}, {}, {}
stopped = set()  # keys stopped from a button (their exit code is not a failure)
wants = set()  # rows whose Theme watch is on; base watch runs while any is on
lock = threading.Lock()
ddev = {"running": False, "busy": False}
RUN_ID = time.time()  # changes on every script start; the page reloads when it sees a new one
boot = {"ready": False, "step": "Starting…"}  # shown on the loading screen until startup is done
seen = threading.Event()  # set when a browser tab polls; an old tab reconnecting = no new tab


def run(key, cmd, cwd=PROJECT, on_exit=None):
    """Start cmd in its own process group; capture output into logs[key]."""
    if key in procs and procs[key].poll() is None:
        return
    logs[key] = log = deque(maxlen=400)
    stopped.discard(key)
    p = subprocess.Popen(["bash", "-lc", cmd], cwd=cwd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, start_new_session=True)
    procs[key] = p

    def pump():
        for line in p.stdout:
            logs[key].append(line.rstrip())
        p.wait()
        log.append(f"--- exited ({p.returncode})")
        if on_exit:
            on_exit(p.returncode)
        if p.returncode == 0 or key in stopped:  # clean finish / stopped by us -> close its tab; failures stay
            t = threading.Timer(3, lambda: logs.get(key) is log and logs.pop(key))
            t.daemon = True  # never keeps the script (and its port) alive after Ctrl+C
            t.start()
    threading.Thread(target=pump, daemon=True).start()


def stop(key):
    p = procs.get(key)
    if p and p.poll() is None:
        stopped.add(key)
        os.killpg(p.pid, signal.SIGTERM)


def alive(key):
    return key in procs and procs[key].poll() is None


def external():
    """[(pid, theme, 'watch'|'storybook')] started outside this dashboard (terminal, old dashboard run)."""
    found = []
    if not Path("/proc").is_dir():
        return found  # ponytail: Linux-only scan; on macOS already-running processes are not taken over (lsof/ps if needed)
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")
            cwd = Path(os.readlink(d / "cwd"))
        except OSError:
            continue
        cmd = b" ".join(argv).decode(errors="replace")
        if cwd.parent != THEMES or not argv[0].endswith(b"node"):
            continue
        what = "watch" if "build-components.mjs --watch" in cmd else "storybook" if "storybook dev" in cmd else None
        if what:
            found.append((int(d.name), cwd.name, what))
    return found


def take_over():
    """Stop already-running watch/storybook processes and start the same ones from the dashboard."""
    found = external()
    for pid, theme, what in found:
        boot["step"] = f"Stopping {theme} {what} started outside the dashboard…"
        print(f"stopping {theme} {what} (pid {pid})")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if found:
        boot["step"] = "Waiting for ports to free up…"
        time.sleep(2)  # let ports (storybook) free up
    for theme, what in {(t, w) for _, t, w in found}:
        idx = next((i for i, r in enumerate(ROWS) if r[2] == theme), None)
        if idx is None:
            continue  # theme not on the dashboard (e.g. dco)
        if what == "watch" and idx not in wants:
            boot["step"] = f"Restarting {theme} watch…"
            action("watch", idx)
        elif what == "storybook" and not alive(f"{theme}:storybook"):
            boot["step"] = f"Restarting {theme} storybook…"
            action("storybook", idx)
        print(f"restarted {theme} {what} from the dashboard")


def check_ddev():
    try:
        out = subprocess.run(["ddev", "describe", "-j"], cwd=PROJECT, capture_output=True, text=True, timeout=30).stdout
        ddev["running"] = json.loads(out.strip().splitlines()[-1])["raw"]["status"] == "running"
    except Exception:
        ddev["running"] = False


def watch_ddev():
    """Re-check every 10s, so a ddev started/stopped from a terminal shows up too."""
    while True:
        if not ddev["busy"]:
            check_ddev()
        time.sleep(10)


def ddev_action(action):
    if ddev["busy"]:
        return
    ddev["busy"], ddev["action"] = True, action

    def done(code):
        ddev["busy"] = False
        check_ddev()
    run("ddev", f"ddev {action}", on_exit=done)


def installing(theme):
    return alive(f"{theme}:install") or alive(f"{BASE}:install")


notice = {}  # branch-change banner for the page


def on_commit_change(old, new):
    """After checkout/pull: npm ci where package-lock.json changed, restart running watch/storybook."""
    themes = [r[2] for r in ROWS]
    lock_changed = [t for t in themes if git("diff", "--quiet", old, new, "--",
                                             f"web/themes/custom/{t}/package-lock.json") is None]
    running = [k for k in list(procs) if alive(k) and k.split(":")[1] in ("watch", "storybook")]
    for k in running:
        stop(k)
    for _ in range(30):  # up to 15s for them to exit (frees storybook ports)
        if not any(alive(k) for k in running):
            break
        time.sleep(0.5)
    for t in lock_changed:
        run(f"{t}:install", NVM + "echo '== package-lock.json changed on this branch: running npm ci' && npm ci", THEMES / t)
    while any(alive(f"{t}:install") for t in lock_changed):
        time.sleep(1)
    for k in running:
        theme, what = k.split(":")
        cmd = "npm run watch" if what == "watch" else "npm run storybook -- --ci"
        run(k, NVM + DEPS + cmd, THEMES / theme)
    notice.update(branch=git("rev-parse", "--abbrev-ref", "HEAD") or "?", restarted=len(running),
                  installed=lock_changed, at=time.time())


def watch_git():
    """Poll HEAD every 2s; act once it has been stable for 3s (a rebase moves it many times)."""
    seen_head = git("rev-parse", "HEAD")
    while True:
        time.sleep(2)
        head = git("rev-parse", "HEAD")
        if not head or head == seen_head:
            continue
        time.sleep(3)
        if git("rev-parse", "HEAD") != head:
            continue  # still moving, check again next round
        old, seen_head = seen_head, head
        notice.update(prev=git("name-rev", "--name-only", old) or old[:8])
        on_commit_change(old, head)


def action(name, idx):
    label, alias, theme, sb = ROWS[idx]
    tdir = THEMES / (theme or "")
    key = f"{theme or alias}:{name}"
    if name in ("watch", "build", "storybook") and installing(theme):
        return key  # npm ci after a branch switch is still running
    if name == "watch":
        if idx in wants:
            wants.discard(idx)
            if theme != BASE:
                stop(key)
            if not wants:
                stop(f"{BASE}:watch")
        else:
            wants.add(idx)
            run(f"{BASE}:watch", NVM + DEPS + "npm run watch", THEMES / BASE)
            if theme != BASE:
                run(key, NVM + DEPS + "npm run watch", tdir)
    elif name == "build":
        if theme != BASE and alive(f"{BASE}:watch"):  # base watch already keeps undco_theme built
            run(key, f"{NVM}echo '== {BASE} watch is running, building {theme} only' && {DEPS}npm run build", tdir)
        else:
            sub = "" if theme == BASE else f" && cd {tdir} && echo '== {theme}' && {DEPS}npm run build"
            run(key, f"{NVM}echo '== {BASE}' && {DEPS}npm run build{sub}", THEMES / BASE)
    elif name == "storybook":
        stop(key) if alive(key) else run(key, NVM + DEPS + "npm run storybook -- --ci", tdir)
    elif name == "open-sb":
        webbrowser.open(f"http://localhost:{sb}")
    elif name == "deploy":
        if alive(key):
            return key
        if previewed.pop(idx, None) != snapshot(alias):  # branch/files changed after the dialog, or no preview
            logs[key] = deque(["Not run: the branch or this site's config files changed since the preview.",
                               "Click Update Site again to see the current changes."], maxlen=400)
            return key
        run(key, f"ddev drush @{alias}.local deploy -y")  # updb -> cim -> cr -> deploy hooks
    elif name == "cr":
        run(key, f"ddev drush @{alias}.local cr")
    elif name == "uli":
        uli.pop(idx, None)  # old link disappears while the new one is generated
        out = subprocess.run(["ddev", "drush", f"@{alias}.local", "uli"], cwd=PROJECT,
                             capture_output=True, text=True).stdout.strip()
        uli[idx] = out if out.startswith("http") else ""
        if not uli[idx]:  # success shows in the row; only a failure gets a log tab
            logs[key] = deque([out or "uli failed (is ddev running?)"], maxlen=400)
    return key


def git(*args):
    """Run git in the project; None if git is missing or fails (callers must not treat that as 'clean')."""
    try:
        out = subprocess.run(["git", *args], cwd=PROJECT, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def snapshot(alias):
    """Commit + this site's config/ status; Update Site refuses if it changed since the preview."""
    return git("rev-parse", "HEAD"), git("status", "--short", "--", f"config/{alias}/", "config/shared/")


previewed = {}  # row idx -> snapshot taken when its preview was shown


def preview(idx, force=False):
    """Checks before Update Site: DDEV up, no stray config changes for this site, then list what cim would change."""
    alias = ROWS[idx][1]
    if not ddev["running"]:
        return {"error": "DDEV is not running. Start it first."}
    dirty = git("status", "--short", "--", f"config/{alias}/", "config/shared/")  # only folders this site imports
    if dirty is None:
        return {"error": "Could not run git status, so the config safety check can't be done."}
    if dirty and not force:  # e.g. left over from a config export, or your own new config not committed yet
        return {"dirty": dirty[:1500]}
    try:
        out = subprocess.run(["ddev", "drush", f"@{alias}.local", "config:import", "--no"], cwd=PROJECT,
                             capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"error": "The config preview took over 2 minutes and was stopped. Is DDEV healthy?"}
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out.stdout + out.stderr)
    previewed[idx] = snapshot(alias)
    if "no changes" in text.lower():
        return {"changes": ""}
    rows = [re.sub(r"[|\s]+", " ", l).strip() for l in text.splitlines() if re.search(r"\b(create|update|delete|rename)\b", l, re.I)]
    if not rows:
        return {"error": "Could not preview the config import:\n\n" + text.strip()[-1500:]}
    if any("config_split.config_split." in l and re.search("create", l, re.I) for l in rows):
        previewed.pop(idx, None)  # typical right after importing a prod dump
        return {"error": "config_split is not set up in this site's database (usual after importing a prod dump). "
                         "Importing now can uninstall dblog and crash the site.\n\nFix first:\n"
                         f"1. ddev drush @{alias}.local pm:install config_split -y\n"
                         f"2. ddev drush @{alias}.local php:eval '$s=\\Drupal::service(\"config.storage.sync\");"
                         "$a=\\Drupal::service(\"config.storage\");foreach($s->listAll(\"config_split.config_split.\") as $n)"
                         "$a->write($n,$s->read($n));'\n"
                         f"3. ddev drush @{alias}.local cr\n"
                         "4. Click Update Site again"}
    counts = {op: sum(bool(re.search(op, l, re.I)) for l in rows) for op in ("create", "update", "delete")}
    return {"changes": "\n".join(rows[:30]) + ("\n…" if len(rows) > 30 else ""), "counts": counts}


def port_open(port):
    """True once something accepts connections on localhost:port (storybook is up)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def status():
    rows = []
    for i, (label, alias, theme, sb) in enumerate(ROWS):
        k = theme or alias
        by = [ROWS[j][0] for j in list(wants) if j != i] if theme == BASE and i not in wants else []
        rows.append({"i": i, "label": label, "site": bool(alias), "theme": theme, "sb": sb,
                     "watch": i in wants or (theme == BASE and alive(f"{BASE}:watch")), "watch_by": by,
                     "now": [a for a in ("watch", "build", "storybook", "cr", "deploy") if alive(f"{k}:{a}")],
                     "build": alive(f"{k}:build"),
                     "installing": bool(theme) and installing(theme), "storybook": alive(f"{k}:storybook"), "sb_ready": alive(f"{k}:storybook") and port_open(sb), "cr": alive(f"{k}:cr"), "uli": uli.get(i, "")})
    names = {**{r[2]: r[0] for r in ROWS}, **{r[1]: r[0] for r in ROWS}}  # theme/alias -> site label
    tabs = []
    for k in list(logs):  # copy: other threads add/remove logs meanwhile
        who, _, what = k.partition(":")
        tabs.append({"key": k, "label": f"{names.get(who, who)} · {what}" if what else "DDEV", "alive": alive(k)})
    return {"ddev": ddev, "rows": rows, "tabs": tabs, "boot": boot, "run_id": RUN_ID, "notice": notice, "branch": git("rev-parse", "--abbrev-ref", "HEAD") or "?"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] == "/":
            return self.send(PAGE.replace("{{PROJECT}}", str(PROJECT)).replace("{{THEME}}", load_config().get("theme", "")), "text/html")
        if self.path == "/status":
            seen.set()
            return self.send(status())
        if self.path.startswith("/log/"):
            key = self.path[5:]
            return self.send("\n".join(logs.get(self.path[5:], [])), "text/plain")
        self.send_error(404)

    def do_POST(self):
        parts = self.path.strip("/").split("/")
        if parts[0] == "ddev" and parts[1] in ("start", "stop", "restart"):
            ddev_action(parts[1])
            return self.send({"key": "ddev"})
        if parts[0] == "uli-used":
            uli.pop(int(parts[1]), None)
            return self.send({"key": ""})
        if parts[0] == "notice":
            notice.clear()
            return self.send({"key": ""})
        if parts[0] == "theme" and parts[1] in ("light", "dark"):
            save_config(theme=parts[1])
            return self.send({"key": ""})
        if parts[0] == "preview":
            return self.send(preview(int(parts[1]), force=parts[2:] == ["force"]))
        if parts[0] == "act":
            return self.send({"key": action(parts[1], int(parts[2]))})
        self.send_error(404)


PAGE = """<!doctype html><html data-theme="{{THEME}}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>UNDCO Dev Dashboard</title>
<script>if(!document.documentElement.dataset.theme)delete document.documentElement.dataset.theme</script>
<style>
:root{--bg:#f5f6f8;--panel:#fff;--panel2:#f0f2f5;--text:#171a1f;--muted:#6b7280;--border:#e4e7ec;
  --accent:#4f46e5;--accent-text:#fff;--accent-soft:#eef0ff;--ok:#15803d;--ok-soft:#dcfce7;--warn:#b45309;--warn-soft:#fef3c7;
  --bad:#b91c1c;--term:#0f1218;--term-text:#d0d5dd;--shadow:0 1px 2px rgba(16,24,40,.05),0 6px 20px rgba(16,24,40,.05);--btn:#fff;--btn-hover:#f7f8fa}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#111318;--panel:#1b1f27;--panel2:#262b36;--text:#f3f4f6;--muted:#b4bccb;
  --border:#3a4150;--accent:#a5a6ff;--accent-text:#0b0d11;--accent-soft:#2c2f5c;--ok:#5ee68f;--ok-soft:#173d27;--warn:#fcd34d;--warn-soft:#45360f;
  --bad:#fca5a5;--term:#0a0c10;--term-text:#e2e8f0;--shadow:0 1px 3px rgba(0,0,0,.5);--btn:#2a303c;--btn-hover:#343b49}}
:root[data-theme="dark"]{--bg:#111318;--panel:#1b1f27;--panel2:#262b36;--text:#f3f4f6;--muted:#b4bccb;
  --border:#3a4150;--accent:#a5a6ff;--accent-text:#0b0d11;--accent-soft:#2c2f5c;--ok:#5ee68f;--ok-soft:#173d27;--warn:#fcd34d;--warn-soft:#45360f;
  --bad:#fca5a5;--term:#0a0c10;--term-text:#e2e8f0;--shadow:0 1px 3px rgba(0,0,0,.5);--btn:#2a303c;--btn-hover:#343b49}
*{box-sizing:border-box}
@supports not selector(::-webkit-scrollbar){*{scrollbar-width:thin;scrollbar-color:color-mix(in srgb,var(--muted) 45%,transparent) transparent}pre{scrollbar-color:#3b4250 transparent}}  /* Firefox */
*::-webkit-scrollbar{width:8px;height:8px}*::-webkit-scrollbar-button{display:none}*::-webkit-scrollbar-corner{background:transparent}*::-webkit-scrollbar-track{background:transparent}
*::-webkit-scrollbar-thumb{background:color-mix(in srgb,var(--muted) 45%,transparent);border-radius:8px;border:2px solid transparent;background-clip:padding-box}
*::-webkit-scrollbar-thumb:hover{background-color:color-mix(in srgb,var(--muted) 70%,transparent)}
pre::-webkit-scrollbar-thumb{background-color:#3b4250}pre::-webkit-scrollbar-thumb:hover{background-color:#5a6274}
#tabs{scrollbar-width:none}#tabs::-webkit-scrollbar{display:none}  /* tabs still scroll by swipe / shift+wheel */
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Ubuntu,sans-serif}
.wrap{max-width:1240px;margin:0 auto;padding:28px 20px 40px}
header{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:20px}
.brand{display:flex;align-items:center;gap:12px}
.logo{width:38px;height:38px;border-radius:10px;display:grid;place-items:center;background:linear-gradient(135deg,var(--accent),#06b6d4);color:#fff;font-weight:700}
h1{font-size:18px;margin:0;letter-spacing:-.01em}.sub{margin:0;color:var(--muted);font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.card{background:var(--panel);border:1px solid color-mix(in srgb,var(--accent) 22%,var(--border));border-radius:14px;box-shadow:var(--shadow);transition:border-color .2s,box-shadow .2s}
.ddev{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;padding:14px 18px;margin-bottom:16px}
.ddev .l{display:flex;align-items:center;gap:12px}.k{font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.pill{display:inline-flex;align-items:center;gap:7px;padding:4px 10px;border-radius:999px;background:var(--panel2);color:var(--muted);font-size:12px;font-weight:600}
.pill::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.pill.ok{background:var(--ok-soft);color:var(--ok)}.pill.warn{background:var(--warn-soft);color:var(--warn)}
.pill.ok::before,.pill.warn::before{animation:pulse 1.6s ease-in-out infinite}
.grp{display:flex;gap:8px;flex-wrap:wrap}
.b{display:inline-flex;align-items:center;justify-content:center;gap:6px;height:32px;padding:0 13px;border-radius:8px;border:1px solid color-mix(in srgb,var(--accent) 30%,var(--border));
  background:var(--btn);color:var(--text);font-family:inherit;font-size:13px;font-weight:500;line-height:1;cursor:pointer;text-decoration:none;white-space:nowrap;transition:border-color .15s,background .15s,transform .05s}
.b:hover{border-color:var(--accent);background:var(--btn-hover)}.b:active{transform:translateY(1px)}
.b.sm{height:28px;padding:0 10px;font-size:12px}
.b.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-text)}
.b.on{background:var(--ok-soft);border-color:color-mix(in srgb,var(--ok) 35%,transparent);color:var(--ok)}
.b.on::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor;animation:pulse 1.6s ease-in-out infinite}
.b.busy{background:var(--warn-soft);border-color:color-mix(in srgb,var(--warn) 35%,transparent);color:var(--warn)}
.b:disabled{cursor:default}.b.icon{width:34px;padding:0;font-size:16px}
.sites{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:16px;margin-bottom:16px}
.site{position:relative;overflow:hidden;padding:18px 18px 14px}
.site::before{content:"";position:absolute;inset:0 0 auto;height:3px;background:linear-gradient(90deg,var(--accent),#06b6d4)}
.site:hover{border-color:color-mix(in srgb,var(--accent) 60%,var(--border));box-shadow:var(--shadow),0 0 0 3px var(--accent-soft)}
.site-h{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:10px}
.site-h b{font-size:15px;letter-spacing:-.01em}
.branch{margin-left:8px;padding:1px 8px;border-radius:6px;background:var(--accent-soft);color:var(--accent);font-weight:600}
.notice{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 16px;margin-bottom:16px;border-color:color-mix(in srgb,var(--warn) 45%,var(--border));background:var(--warn-soft)}
.notice b{color:var(--warn)}
.inst{margin-left:8px;color:var(--warn);font-size:12px;font-weight:600}
.chip{display:inline-block;margin-left:8px;padding:2px 8px;border-radius:6px;background:var(--accent-soft);color:var(--accent);font:600 11px ui-monospace,SFMono-Regular,Menlo,monospace;vertical-align:1px}
.uli{display:flex;gap:6px;margin:0 0 10px;border-radius:10px}.uli.fresh{animation:fresh 1.4s ease-out}
@keyframes fresh{0%{box-shadow:0 0 0 3px var(--accent)}100%{box-shadow:0 0 0 3px transparent}}
.uli input{flex:1;min-width:0;height:28px;padding:0 8px;border-radius:8px;border:1px solid var(--border);background:var(--panel2);color:var(--muted);font:11px ui-monospace,monospace}
.act{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 0;border-top:1px solid var(--border)}
.act .n{min-width:0}.act .t{font-weight:600;font-size:13px}
.now{color:var(--ok);font-size:12px;font-weight:500}
.idle{color:var(--muted);font-size:12px}
.act .grp{flex-wrap:nowrap;flex:none}.act .grp .b{width:140px;overflow:hidden;text-overflow:ellipsis}.b::before{flex:none}.act .grp .b.ext{width:32px;padding:0}
.logs{overflow:hidden}
.logs-h{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 12px;border-bottom:1px solid var(--border)}
#tabs{display:flex;gap:6px;overflow-x:auto;min-width:0;padding-right:24px;mask-image:linear-gradient(90deg,#000 calc(100% - 24px),transparent)}
#tabs .b{height:28px;font-size:12px;background:transparent;border-color:transparent;color:var(--muted)}
#tabs .b:hover{background:var(--panel2)}#tabs .b.sel{background:var(--panel2);color:var(--text);border-color:var(--border)}
#tabs .dot{width:7px;height:7px;border-radius:50%;background:var(--muted);opacity:.7}#tabs .dot.live{background:var(--ok);opacity:1;animation:pulse 1.6s infinite}
pre{margin:0;background:var(--term);color:var(--term-text);padding:14px 16px;height:320px;overflow:auto;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
pre .a1{font-weight:700}pre .a2{opacity:.6}pre .a3{font-style:italic}pre .a4{text-decoration:underline}pre .a31,pre .a91{color:#f87171}pre .a32,pre .a92{color:#4ade80}pre .a33,pre .a93{color:#facc15}pre .a34,pre .a94{color:#60a5fa}pre .a35,pre .a95{color:#c084fc}pre .a36,pre .a96{color:#22d3ee}pre .a37,pre .a97{color:#f3f4f6}pre .a90{color:#9ca3af}
pre:empty::before{content:"No output yet. Start something above and its log shows here.";color:#8b93a3}
.mini{display:inline-block;vertical-align:-1px;width:11px;height:11px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:spin .7s linear infinite}
.overlay{position:fixed;inset:0;display:none;flex-direction:column;align-items:center;justify-content:center;gap:14px;z-index:9;text-align:center;padding:24px}
#loading{display:flex;background:var(--bg);color:var(--text)}#down{background:rgba(8,10,14,.72);color:#fff;font-size:16px;backdrop-filter:blur(3px)}
.spin{width:38px;height:38px;border:3px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite}
#step{color:var(--muted)}
@keyframes spin{to{transform:rotate(360deg)}}@keyframes pulse{50%{opacity:.35}}
::view-transition-old(root),::view-transition-new(root){animation:none;mix-blend-mode:normal}
::view-transition-new(root){z-index:2;-webkit-mask-image:radial-gradient(closest-side,#000 55%,transparent 100%);mask-image:radial-gradient(closest-side,#000 55%,transparent 100%);-webkit-mask-repeat:no-repeat;mask-repeat:no-repeat}
@media (max-width:560px){.wrap{padding:18px 16px}.sites{grid-template-columns:1fr}}
</style></head><body>
<div id="loading" class="overlay"><div class="spin"></div><div id="step">Starting…</div></div>
<div id="down" class="overlay">Dashboard stopped.<br><small style="opacity:.75">Close this tab, or start the script again and it reconnects here.</small></div>
<div class="wrap">
<header>
  <div class="brand"><div class="logo">U</div><div><h1>UNDCO Dev Dashboard</h1><p class="sub">{{PROJECT}} <span class="branch" title="Current git branch">⎇ <span id="branch">…</span></span></p></div></div>
  <button id="themeBtn" class="b icon" title="Toggle light / dark" onclick="toggleTheme()">◐</button>
</header>
<section class="card ddev">
  <div class="l"><span class="k">DDEV</span><span id="st" class="pill">…</span></div>
  <div class="grp">
    <button id="bstart" class="b" onclick="post('/ddev/start')">Start</button>
    <button id="bstop" class="b" onclick="post('/ddev/stop')">Stop</button>
    <button id="brestart" class="b" onclick="post('/ddev/restart')">Restart</button>
  </div>
</section>
<div id="notice" class="card notice" style="display:none"></div>
<div id="rows" class="sites"></div>
<section class="card logs">
  <div class="logs-h"><div id="tabs"></div>
    <button class="b sm" onclick="navigator.clipboard.writeText(log.textContent);this.textContent='Copied!';setTimeout(()=>this.textContent='Copy Log',1500)">Copy Log</button></div>
  <pre id="log"></pre>
</section>
</div>
<script>
function toggleTheme(){
  const r=document.documentElement,dark=r.dataset.theme?r.dataset.theme==='dark':matchMedia('(prefers-color-scheme: dark)').matches;
  const apply=()=>{r.dataset.theme=dark?'light':'dark';fetch('/theme/'+r.dataset.theme,{method:'POST'})};
  if(!document.startViewTransition||matchMedia('(prefers-reduced-motion: reduce)').matches)return apply();
  // new theme grows as a circle from the toggle button until it covers the screen
  // soft-edged mask (solid centre, feathered rim) scaled up from the button, so there is no hard circle line
  const b=themeBtn.getBoundingClientRect(),x=b.left+b.width/2,y=b.top+b.height/2,
        R=Math.hypot(Math.max(x,innerWidth-x),Math.max(y,innerHeight-y))/0.55;  // solid part (55%) must reach the far corner
  const size=[`0px 0px`,`${2*R}px ${2*R}px`],pos=[`${x}px ${y}px`,`${x-R}px ${y-R}px`];
  document.startViewTransition(apply).ready.then(()=>r.animate(
    {maskSize:size,webkitMaskSize:size,maskPosition:pos,webkitMaskPosition:pos},
    {duration:900,easing:'cubic-bezier(.4,0,.2,1)',fill:'both',pseudoElement:'::view-transition-new(root)'}));
}
let runId=0,logKey='ddev',seenTabs=[];  // seenTabs: tabs viewed before, newest last
function show(k){if(k!==logKey){seenTabs=seenTabs.filter(x=>x!==logKey);seenTabs.push(logKey);logKey=k}}
const checking=new Set();
async function deploy(i,label){
  checking.add(i);refresh();
  try{
    let r=await (await fetch('/preview/'+i,{method:'POST'})).json();
    if(r.dirty){
      if(!confirm(`${label}: these config files have uncommitted changes:\n\n${r.dirty}\n\nIf they are left over from a config export, Cancel and restore them.\nIf they are your own work on this branch, OK imports them anyway.`))return;
      r=await (await fetch('/preview/'+i+'/force',{method:'POST'})).json();
    }
    if(r.error)return alert(label+': '+r.error);
    if(r.changes){const c=r.counts;
      if(!confirm(`${label}: config to import\n${c.create} create · ${c.update} update · ${c.delete} delete${c.delete?'   ⚠ includes deletes':''}\n\n${r.changes}\n\nRun updb → cim → cr now?`))return}
    await post('/act/deploy/'+i);
  }finally{checking.delete(i);refresh()}
}
async function post(u){const r=await (await fetch(u,{method:'POST'})).json();show(r.key);refresh()}
const genUli=new Set(),shownUli=new Set();  // shownUli: flash a link only the first time it appears
async function newUli(i){genUli.add(i);refresh();try{await post('/act/uli/'+i)}finally{genUli.delete(i);refresh()}}
function usedUli(i){fetch('/uli-used/'+i,{method:'POST'}).then(refresh)}  // one-time link: gone once used
async function copyUli(b,i,u){await navigator.clipboard.writeText(u);b.textContent='Copied!';b.classList.add('on');setTimeout(()=>usedUli(i),2000)}
const SP='<span class="mini"></span>';
function ansi(t){  // terminal colour codes -> coloured spans
  t=t.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));let open=0;
  t=t.replace(/\\x1b\\[([0-9;]*)m/g,(_,c)=>{let o='</span>'.repeat(open);open=0;
    const cls=c.split(';').filter(x=>x&&!['0','22','23','24','39','49'].includes(x)).map(x=>'a'+x).join(' ');
    if(cls){open=1;o+=`<span class="${cls}">`}return o});
  return t.replace(/\\x1b\\[[0-9;?]*[A-Za-z]/g,'')+'</span>'.repeat(open)}
function html(el,h){if(el._h!==h){el._h=h;el.innerHTML=h}}  // re-render only on change (no flicker)
function btn(txt,act,i,state,extra=''){return `<button class="b ${state}" onclick="post('/act/${act}/${i}')">${txt}</button>${extra}`}
function act(title,a,r,control){return `<div class="act"><div class="n"><div class="t">${title}</div>${a==='storybook'&&r.storybook&&!r.sb_ready?'<span class="idle">Starting…</span>':r.now.includes(a)?'<span class="now">Running</span>':'<span class="idle">Idle</span>'}</div><div class="grp">${control}</div></div>`}
async function refresh(){
  const s=await (await fetch('/status')).json(),d=s.ddev;
  if(runId&&s.run_id!==runId)return location.reload();runId=s.run_id;  // script restarted -> load its new page
  loading.style.display=s.boot.ready?'none':'flex';step.textContent=s.boot.step;
  if(!s.boot.ready)return;
  st.textContent=d.busy?({start:'Spinning up…',stop:'Stopping…',restart:'Restarting…'}[d.action]):(d.running?'Running':'Stopped');
  branch.textContent=s.branch;
  const n=s.notice;notice.style.display=n.at?'flex':'none';
  if(n.at)html(notice,`<div><b>Branch changed${n.prev?': '+n.prev+' → ':': '}${n.branch}</b><br>
    ${n.restarted?n.restarted+' Watch/Storybook process(es) restarted. ':''}${n.installed.length?'npm ci ran for '+n.installed.join(', ')+'. ':''}Click <b>Update Site</b> on the sites you use to apply the new config and DB updates.</div>
    <button class="b sm" onclick="fetch('/notice',{method:'POST'}).then(refresh)">Dismiss</button>`);
  st.className='pill '+(d.busy?'warn':d.running?'ok':'');
  bstart.className='b '+(d.busy&&d.action==='start'?'busy':d.running?'on':'primary');
  bstop.className='b '+(d.busy&&d.action==='stop'?'busy':'');brestart.className='b '+(d.busy&&d.action==='restart'?'busy':'');
  html(rows,s.rows.map(r=>`<div class="card site">
   <div class="site-h"><div><b>${r.label}</b><span class="chip">${r.theme}</span>${r.installing?'<span class="inst">'+SP+' Installing packages…</span>':''}</div><button class="b sm ${genUli.has(r.i)?'busy':''}" onclick="newUli(${r.i})">${genUli.has(r.i)?SP+'Generating…':'ULI'}</button></div>
   ${r.uli?`<div class="uli ${shownUli.has(r.uli)?'':(shownUli.add(r.uli),'fresh')}"><input readonly value="${r.uli}"><button class="b sm" onclick="copyUli(this,${r.i},'${r.uli}')">Copy</button><a class="b sm primary" href="${r.uli}" target="_blank" onclick="usedUli(${r.i})">Browse</a></div>`:''}
   ${act('Watch','watch',r,r.watch_by.length?`<button class="b on" disabled title="Used by ${r.watch_by.join(', ')}">${r.watch_by.length>1?'Used By 2 Sites':'Used By '+r.watch_by[0]}</button>`:btn(r.watch?'Stop Watch':'Watch','watch',r.i,r.watch?'on':''))}
   ${act('Build','build',r,btn(r.build?SP+'Building…':'Build','build',r.i,r.build?'busy':''))}
   ${act('Storybook <span class="idle">:'+r.sb+'</span>','storybook',r,(r.sb_ready?`<button class="b ext" title="Open localhost:${r.sb}" onclick="post('/act/open-sb/${r.i}')">↗</button>`:'')+btn(!r.storybook?'Storybook':r.sb_ready?'Stop Storybook':SP+'Starting…','storybook',r.i,!r.storybook?'':r.sb_ready?'on':'busy'))}
   ${act('Database','deploy',r,`<button class="b ${r.now.includes('deploy')||checking.has(r.i)?'busy':''}" title="updb → cim → cr" onclick="deploy(${r.i},'${r.label}')">${r.now.includes('deploy')?SP+'Updating…':checking.has(r.i)?SP+'Checking…':'Update Site'}</button>`)}
   ${act('Cache','cr',r,btn(r.cr?SP+'Clearing…':'Clear Cache','cr',r.i,r.cr?'busy':''))}
  </div>`).join(''));
  const has=k=>s.tabs.some(t=>t.key===k);
  if(!has(logKey)){seenTabs=seenTabs.filter(has);logKey=seenTabs.pop()||(s.tabs[0]||{}).key||logKey}  // closed tab -> back to the previous one
  html(tabs,s.tabs.map(t=>`<button class="b ${t.key===logKey?'sel':''}" onclick="show('${t.key}');refresh()"><span class="dot ${t.alive?'live':''}"></span>${t.label}</button>`).join(''));
  const t=await (await fetch('/log/'+logKey)).text(),atEnd=log.scrollTop+log.clientHeight>=log.scrollHeight-5;
  if(log._t!==t){log._t=t;log.innerHTML=ansi(t);if(atEnd)log.scrollTop=log.scrollHeight}
}
// Tab opened by the script (?new=1) closes itself if a dashboard tab is already open.
const bc=new BroadcastChannel('undco-dashboard'),isNew=location.search.includes('new=1');
history.replaceState(null,'','/');
bc.onmessage=e=>{if(e.data==='hello'){bc.postMessage('here');tick()}  // script restarted: check now -> reloads
  else if(e.data==='here'&&isNew){window.close();
  document.body.innerHTML='<p style="font:18px system-ui;padding:24px">Dashboard is already open in another tab, you can close this one.</p>';}};
if(isNew)bc.postMessage('hello');
let wasDown=false,fails=0;
async function tick(){
  try{await refresh();fails=0;if(wasDown)location.reload();}
  catch(e){
    if(!/fetch|network/i.test(e.message)){console.error(e);return}  // page bug, not a stopped server
    if(++fails>=3){wasDown=true;down.style.display='flex'}       // 3 misses in a row = really stopped
  }
}
setInterval(tick,1000);tick();
tabs.addEventListener('wheel',e=>{if(e.deltaY&&tabs.scrollWidth>tabs.clientWidth){tabs.scrollLeft+=e.deltaY;e.preventDefault()}},{passive:false});  // wheel scrolls tabs sideways
// background tabs get slow timers: catch up the moment the tab is visible again
document.addEventListener('visibilitychange',()=>{if(!document.hidden)tick()});addEventListener('focus',tick);
</script></body></html>"""


def pick_server():
    """Bind PORT, or the next free port that is not a storybook port or a port in .ddev config."""
    reserved = {r[3] for r in ROWS}
    for f in (PROJECT / ".ddev").glob("config*.yaml"):
        for line in f.read_text().splitlines():
            if "port" in line and not line.lstrip().startswith("#"):
                reserved |= {int(n) for n in re.findall(r"\b\d{2,5}\b", line)}
    for port in range(PORT, PORT + 200):
        if port in reserved:
            continue
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), H)
        except OSError:
            pass
    sys.exit(f"No free port between {PORT} and {PORT + 199}")


if __name__ == "__main__":
    srv = pick_server()
    PORT = srv.server_address[1]

    def startup():
        boot["step"] = "Checking DDEV status…"
        check_ddev()
        threading.Thread(target=watch_ddev, daemon=True).start()
        threading.Thread(target=watch_git, daemon=True).start()
        boot["step"] = "Looking for watch/storybook processes already running…"
        take_over()
        boot["ready"] = True
        print("Ready.")
    threading.Thread(target=startup, daemon=True).start()
    print(f"Dashboard: http://localhost:{PORT}  (Ctrl+C stops it and all watch/storybook processes)")
    threading.Thread(target=lambda: seen.wait(5) or webbrowser.open(f"http://localhost:{PORT}/?new=1"), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        for k, p in list(procs.items()):  # never kill an update midway (half-imported config / updb)
            if k.endswith(":deploy") and p.poll() is None:
                print(f"Waiting for {k} to finish (Ctrl+C again to force)...")
                p.wait()
        for k in list(procs):
            if k != "ddev":
                stop(k)
        srv.server_close()
        os._exit(0)  # exit now so the port is free for the next start
