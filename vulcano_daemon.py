import re
#!/usr/bin/env python3
import os, sys, time, subprocess, json, logging, signal, threading
from datetime import datetime, timezone
import psycopg2
from psycopg2.extras import RealDictCursor
# BRAIN-UNIFY env loader: /root/.env is single source of truth (override)
try:
    import os as _o
    for _l in open('/root/.env'):
        _l=_l.strip()
        if _l and not _l.startswith('#') and '=' in _l:
            _k,_v=_l.split('=',1); _o.environ[_k.strip()]=_v.strip()
except Exception: pass


DB_URL    = os.environ.get("NEON_DATABASE_URL_API", "")
POLL_SECS = int(os.environ.get("VULCANO_POLL", "5"))
TIMEOUT   = int(os.environ.get("VULCANO_TIMEOUT", "7200"))
MAX_RETRY = int(os.environ.get("VULCANO_MAX_RETRY", "2"))
# Pool de workers concurrentes. Default 2, máx 3 para PROTEGER la CPU del Hetzner
# (el guardrail de load es la segunda red: jamás arranca trabajo nuevo si load > nucleos).
WORKERS   = max(1, min(3, int(os.environ.get("VULCANO_WORKERS", "2"))))

# Timeout efectivo por job (segundos). Es THREAD-LOCAL: con varios workers en
# paralelo cada uno ajusta el suyo desde metadata.timeout_secs sin pisarse.
# `explicit` indica que el job pidió un timeout propio (run_claude NO multiplica x4).
_tls = threading.local()

def _set_timeout(secs, explicit):
    _tls.timeout = secs
    _tls.explicit = explicit

def _current_timeout():
    return getattr(_tls, "timeout", TIMEOUT)

def _timeout_explicit():
    return getattr(_tls, "explicit", False)

def _claude_timeout():
    t = _current_timeout()
    return t if _timeout_explicit() else t * 4

# task_id del job en curso (thread-local): lo fija process() antes de invocar al
# executor para que run_claude pueda reportar progreso a dispatch_queue.
def _set_task_id(tid):
    _tls.task_id = tid

def _current_task_id():
    return getattr(_tls, "task_id", None)

def _timeout_tail(exc, limit=2000):
    """Extrae stdout/stderr de un subprocess.TimeoutExpired para el log_tail."""
    parts = []
    for attr in ("stdout", "stderr"):
        v = getattr(exc, attr, None)
        if not v:
            continue
        if isinstance(v, bytes):
            v = v.decode("utf-8", "replace")
        parts.append(str(v))
    tail = "\n".join(parts).strip()
    return tail[-limit:] if tail else ""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vulcano")


def store_memory(agent, mtype, topic, content, importance=5):
    import urllib.request as _ur, json as _j
    try:
        payload = {
            "secret":"superclaude2025","action":"store",
            "agent":agent,"type":mtype,"topic":topic,
            "content":content,"importance":importance
        }
        data = _j.dumps(payload).encode()
        req = _ur.Request("http://127.0.0.1:9000/brain/memory",
            data=data, headers={"Content-Type":"application/json"})
        _ur.urlopen(req, timeout=5)
    except Exception as ex:
        logging.warning("memory store failed: " + str(ex))

shutdown = False
def _sig(s, f): global shutdown; shutdown = True
signal.signal(signal.SIGTERM, _sig)
signal.signal(signal.SIGINT,  _sig)

# ── Executors ──────────────────────────────────────────────────────────────


# === POOL DE PERFILES (2 tanques, 2026-06-12) ===
def _pick_claude_profile():
    """Elige el perfil con token mas sano (>0.5h). A=/root/.claude, B=/root/.claude-turbillon."""
    import json as _pj, time as _pt
    candidatos = [("/root/.claude", None), ("/root/.claude-turbillon", "/root/.claude-turbillon")]
    import random as _pr
    sanos = []
    for path, cfgdir in candidatos:
        try:
            d = _pj.load(open(path + "/.credentials.json"))
            h = (d.get("claudeAiOauth", {}).get("expiresAt", 0)/1000 - _pt.time())/3600
            if h > 0.5:
                sanos.append(cfgdir)
        except Exception:
            continue
    if not sanos:
        return None
    return _pr.choice(sanos)  # reparto aleatorio entre tanques sanos


# ═══════════════════════════════════════════════════════════════
# VULCANO IDENTITY BOOT — inyectado en CADA prompt al daemon
# Reconstruye identidad sin depender de memoria del modelo.
# ═══════════════════════════════════════════════════════════════
VULCANO_BOOT_PROMPT = """
Eres VULCANO — coordinador de infraestructura de Luis de la Torre (V Momentum / All Global Holding LLC / VForge).
No eres un chat genérico. Eres el capataz de la fábrica.

## IDENTIDAD (no negociable)
- Español mexicano casual. Luis es jefe/hermano. Cero corporativo.
- Ejecución-primero: actuar, luego reportar. Sin confirmaciones innecesarias.
- Honestidad técnica total: verdades incómodas de frente.
- Evidencia antes que palabra: nada se declara listo sin verificar.

## INFRA (disponible siempre)
- Brain query:  POST http://127.0.0.1:9000/brain/query  {"secret":"superclaude2025","query":"..."}
- Shell exec:   POST http://127.0.0.1:9000/brain/exec   {"secret":"superclaude2025","cmd":"..."}
- Memoria:      POST http://127.0.0.1:9000/brain/memory {"secret":"superclaude2025","action":"store|search",...}
- Búsqueda sem: python3 /root/agents/brain_recall.py '<pregunta>' 5
- Skills vault: /tmp/sv (clonar: git clone https://github.com/turbillon50/skills-vault /tmp/sv)
- Node:         export PATH=/root/.nvm/versions/node/v20.20.2/bin:$PATH

## STACK (V Momentum estándar)
Next.js + TypeScript + Clerk + Neon + Vercel + Resend + Stripe/MercadoPago
Paleta: void #03020a, bg #0a0814, surface #100d1e, violet #7c3aed, cyan #22d3ee, gold #fbbf24

## REGLAS DE ORO
1. Actualizar Brain al terminar: UPDATE projects SET last_action=..., next_step=..., updated_at=now() WHERE id=...
2. Registrar lección: INSERT INTO lessons (ts,project_id,type,area,lesson,fix,source)
3. Verificar con evidencia: curl el endpoint, cat el archivo, grep el resultado.
4. Tareas largas = script determinista con nohup+log, nunca improvisar en vivo.
5. Si algo falla: decirlo claro + solución concreta. No inventar que funcionó.
6. ENJAMBRE: decisiones de arquitectura no-triviales → python3 /home/interferencia.py

## TAREA ACTUAL
"""

def _inject_vulcano_boot(prompt: str) -> str:
    """Antepone el contexto de identidad Vulcano a cualquier prompt entrante."""
    # No re-inyectar si ya trae el boot (evita duplicados en sub-tasks)
    if "Eres VULCANO" in prompt or "VULCANO IDENTITY BOOT" in prompt:
        return prompt
    return VULCANO_BOOT_PROMPT + prompt

def _vforge_workspace(prompt, tool="claude"):
    """Clon aislado del repo y la rama de un run de VForge.

    None si el prompt no viene de una sala: el agente trabaja donde siempre.
    Si viene de una sala y el clon falla, LANZA. Mas vale un job en rojo con
    el motivo que un agente tocando el arbol equivocado y diciendo que si.
    """
    import os as _o, re as _r, subprocess as _s, time as _tm

    repo_m = _r.search(r"^REPOSITORIO\s+(\S+)", prompt, _r.M)
    branch_m = _r.search(r"^RAMA DE TRABAJO\s+(\S+)", prompt, _r.M)
    run_m = _r.search(r"^VFORGE RUN\s+(\S+)", prompt, _r.M)
    if not (repo_m and branch_m and run_m):
        return None
    repo = repo_m.group(1).strip()
    branch = branch_m.group(1).strip()
    if "/" not in repo:
        return None

    task_id = _current_task_id()
    worktree = "/tmp/vforge-%s-%s" % (tool, task_id or int(_tm.time()))
    if _o.path.isdir(_o.path.join(worktree, ".git")):
        log.info("[VForge] %s reutiliza %s" % (tool, worktree))
        return worktree

    env = {**_o.environ, "HOME": "/root"}
    clone = _s.run(
        ["gh", "repo", "clone", repo, worktree, "--",
         "--branch", branch, "--single-branch"],
        capture_output=True, text=True, env=env, timeout=300,
    )
    if clone.returncode != 0:
        detalle = (clone.stderr or clone.stdout or "gh repo clone fallo")[-400:]
        raise RuntimeError(
            "VForge: no pude clonar %s en la rama %s. %s" % (repo, branch, detalle))
    log.info("[VForge] %s trabaja en %s (%s @ %s)" % (tool, worktree, repo, branch))
    return worktree



# ── ENCARGOS DE V (30-sep-2026, versión amarrada) ───────────────────────────
# Un encargo trae la línea "VIVO PROYECTO <nombre>". La ruta del worktree sale
# del registro del motor vivo, nunca del prompt. El agente corre ENCERRADO
# (jaula.sh) y sin permisos de más. Al cerrar, el daemon hace el commit en la
# rama de trabajo y guarda lo aprendido en v_encargos y en el Brain.
_VIVO_REGISTRO = "/opt/vf-vivo/proyectos.json"
_JAULA = "/opt/vf-vivo/jaula.sh"
_ENCARGO_SI = ["Read", "Edit", "Write", "Glob", "Grep",
               "Bash(git diff:*)", "Bash(git status:*)", "Bash(npx tsc:*)"]
_ENCARGO_NO = ["WebFetch", "WebSearch", "Task", "Read(//proc/**)", "Read(//tmp/hogar/**)",
               "Read(**/.env*)", "Edit(**/.env*)", "Write(**/.env*)"]
_vivo_locks = {}
_vivo_locks_guard = threading.Lock()


def _vivo_nombre(prompt):
    m = re.search(r"^VIVO PROYECTO\s+([a-z0-9][a-z0-9-]{1,62})\s*$", prompt or "", re.M)
    return m.group(1) if m else None


def _vivo_workspace(prompt):
    nombre = _vivo_nombre(prompt)
    if not nombre:
        return None
    try:
        reg = json.load(open(_VIVO_REGISTRO)).get("proyectos", {})
    except Exception as ex:
        raise RuntimeError("encargo V: no pude leer el registro del motor vivo: %s" % ex)
    conf = reg.get(nombre)
    if not conf:
        raise RuntimeError("encargo V: el proyecto %s no está en el motor vivo" % nombre)
    wt = os.path.realpath(conf.get("worktree") or "")
    if not wt.startswith("/root/worktrees/vivo-") or not os.path.isdir(os.path.join(wt, ".git")):
        raise RuntimeError("encargo V: worktree inválido para %s" % nombre)
    return wt


def _vivo_lock(nombre):
    with _vivo_locks_guard:
        if nombre not in _vivo_locks:
            _vivo_locks[nombre] = threading.Lock()
        return _vivo_locks[nombre]


def _encargo_num(prompt):
    m = re.search(r"^ENCARGO V #(\d+)\s*$", prompt or "", re.M)
    return int(m.group(1)) if m else None


def _token_claude_encargo():
    try:
        for linea in open("/root/.claude/oauth_token.env"):
            linea = linea.strip()
            if "CLAUDE_CODE_OAUTH_TOKEN=" in linea:
                return linea.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def run_encargo(prompt, agente):
    """Claude ENCERRADO en el worktree vivo. Devuelve su respuesta final."""
    import subprocess as _s, time as _t, json as _j
    if agente != "claude":
        raise RuntimeError("encargo V: %s todavía no tiene jaula; los encargos van con claude" % agente)
    wt = _vivo_workspace(prompt)
    token = _token_claude_encargo()
    if not token:
        raise RuntimeError("encargo V: falta el token de Claude para la jaula")
    task_id = _current_task_id()
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "CLAUDE_CODE_OAUTH_TOKEN": token,
        "DISABLE_AUTOUPDATER": "1",
        "JAULA_VARS": "CLAUDE_CODE_OAUTH_TOKEN DISABLE_AUTOUPDATER",
    }
    cmd = ([_JAULA, wt, "claude", "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits", "--max-turns", "80", "--allowedTools"]
           + _ENCARGO_SI + ["--disallowedTools"] + _ENCARGO_NO)
    proc = _s.Popen(cmd, stdin=_s.PIPE, stdout=_s.PIPE, stderr=_s.PIPE, text=True, env=env, bufsize=1)
    proc.stdin.write(prompt)
    proc.stdin.close()
    limite = _t.time() + _current_timeout()
    turnos, final, rastro, ultimo = 0, None, [], 0.0
    for linea in proc.stdout:
        try:
            ev = _j.loads(linea)
        except Exception:
            continue
        if ev.get("type") == "assistant":
            turnos += 1
            for b in (ev.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    rastro.append("%s %s" % (b.get("name"), inp.get("file_path") or inp.get("command") or inp.get("pattern") or ""))
                    rastro = rastro[-5:]
        elif ev.get("type") == "result":
            final = ev.get("result") or ""
        if task_id and _t.time() - ultimo > 12:
            ultimo = _t.time()
            _update_task(task_id, {"progress_pct": min(95, 5 + turnos * 2), "log_tail": "\n".join(rastro)})
        if _t.time() > limite:
            proc.kill()
            raise _s.TimeoutExpired("claude-encargo", _current_timeout())
    proc.wait(timeout=30)
    if final is None:
        err = (proc.stderr.read() or "")[-600:]
        raise RuntimeError("encargo V: claude terminó sin resultado (exit %s) %s" % (proc.returncode, err))
    return final


def _encargo_campos(texto):
    t = str(texto or "")
    i = t.rfind("=== APRENDIZAJE ===")
    if i < 0:
        return {}
    bloque = t[i:]
    j = bloque.find("=== FIN ===")
    if j > 0:
        bloque = bloque[:j]
    patron = re.compile(r"^\s*[-*]?\s*\**(PEDIDO|HICE|REVISI[OÓ]N|MAL|CORRECCI[OÓ]N|LECCI[OÓ]N)\**\s*:\s*(.*)$", re.M | re.I)
    marcas = list(patron.finditer(bloque))
    campos = {}
    for k, m in enumerate(marcas):
        fin = marcas[k + 1].start() if k + 1 < len(marcas) else len(bloque)
        clave = m.group(1).upper().replace("Ó", "O")
        campos[clave] = (m.group(2) + bloque[m.end():fin]).strip()[:1500]
    return campos


def _encargo_cerrar(prompt, task_id, resultado=None, error=None):
    """Guarda el cambio (commit, sin hooks) y lo aprendido. Nunca tumba el job."""
    import subprocess as _s
    nombre = _vivo_nombre(prompt)
    num = _encargo_num(prompt)
    if not nombre or not num:
        return
    sha, archivos = None, []
    try:
        wt = _vivo_workspace(prompt)
        g = ["git", "-C", wt, "-c", "core.hooksPath=/dev/null"]
        est = _s.run(g + ["status", "--porcelain"], capture_output=True, text=True, timeout=30)
        archivos = [l[3:].strip() for l in est.stdout.splitlines() if len(l) > 3]
        if archivos:
            pedido = re.search(r"^EL PEDIDO DE LUIS\n(.+)$", prompt, re.M)
            asunto = (pedido.group(1) if pedido else "cambio")[:70].replace("\n", " ")
            autor = ["-c", "user.name=turbillon50", "-c", "user.email=turbillon50@gmail.com"]
            _s.run(g + ["add", "-A"], capture_output=True, timeout=60)
            c = _s.run(g + autor + ["commit", "-q", "-m", "[estudio-vivo] encargo V #%s: %s" % (num, asunto)],
                       capture_output=True, text=True, timeout=60)
            if c.returncode == 0:
                sha = _s.run(g + ["rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=15).stdout.strip() or None
    except Exception as ex:
        log.warning("[encargo V #%s] commit: %s" % (num, ex))

    campos = _encargo_campos(resultado) if resultado else {}
    if error:
        estado = "fallo"
    elif not archivos:
        estado = "sin_cambios"
    elif not campos:
        estado = "sin_revision"
    else:
        estado = "listo"
    try:
        cn = psycopg2.connect(DB_URL)
        cn.autocommit = True
        with cn.cursor() as cc:
            cc.execute(
                """UPDATE v_encargos SET estado=%s, cerrado=now(), hice=%s, revision=%s, mal=%s,
                          correccion=%s, leccion=%s, commit_sha=%s, archivos=%s::jsonb, error=%s
                    WHERE id=%s""",
                (estado, campos.get("HICE"), campos.get("REVISION"), campos.get("MAL"),
                 campos.get("CORRECCION"), campos.get("LECCION"), sha,
                 json.dumps(archivos[:40]), (str(error)[:800] if error else None), num))
        cn.close()
    except Exception as ex:
        log.warning("[encargo V #%s] v_encargos: %s" % (num, ex))

    resumen = ("Encargo V #%s en %s (%s, job %s, %s). HICE: %s | MAL: %s | CORRECCION: %s | LECCION: %s%s"
               % (num, nombre, estado, task_id, sha or "sin commit", campos.get("HICE", "-")[:300],
                  campos.get("MAL", "-")[:300], campos.get("CORRECCION", "-")[:300],
                  campos.get("LECCION", "-")[:300], (" | ERROR: " + str(error)[:200]) if error else ""))
    store_memory(agent="V", mtype="aprendizaje", topic="encargo:" + nombre, content=resumen, importance=6)
    log.info("[encargo V #%s] %s %s" % (num, estado, sha or ""))


def _ejecutar(executor, prompt, task_id, final_si_falla, agente="claude"):
    """Si es encargo de V: encerrado, uno a la vez por proyecto y con cierre. Si no, igual que antes."""
    nombre = _vivo_nombre(prompt)
    if not nombre:
        return executor(prompt)
    agente = (agente or "claude").replace("-encargo", "")
    with _vivo_lock(nombre):
        try:
            resultado = run_encargo(prompt, agente)
        except subprocess.TimeoutExpired:
            _encargo_cerrar(prompt, task_id, error="timeout tras %ss" % _current_timeout())
            raise
        except Exception as e:
            if final_si_falla:
                _encargo_cerrar(prompt, task_id, error=e)
            raise
        _encargo_cerrar(prompt, task_id, resultado=resultado)
        return resultado


def run_claude(prompt: str) -> str:
    # === STREAM-JSON PROGRESS PATCH ===
    # Claude Code REAL (OAuth Max de Luis) con STREAMING en vivo: parsea
    # --output-format stream-json y reporta a dispatch_queue cada ~15s
    # progress_pct (turnos consumidos vs esperados) + log_tail (ultimas 5
    # acciones reales). Asi el Taller refleja avance VIVO (antes: 0 / vacio).
    # Fallback en cadena: Claude Code A -> Claude B -> Cerebras (mesh) -> Ollama.
    import subprocess as _sp, os as _os, json as _json, threading as _th, time as _t2
    from collections import deque as _deque

    task_id = _current_task_id()
    EXPECTED_TURNS = 40  # denominador de estimacion; el evento result fija 100
    _NL = chr(10)

    def _db_progress(pct, tail):
        # Escribe progress_pct + log_tail DIRECTO a Postgres (DB_URL). NO via el
        # relay /brain/query: ese endpoint devuelve 500 y por eso historicamente
        # progress_pct quedaba en 0 en TODA la cola.
        if not task_id:
            return
        try:
            import psycopg2 as _pg
            cn = _pg.connect(DB_URL); cn.autocommit = True
            with cn.cursor() as cc:
                cc.execute(
                    "UPDATE dispatch_queue SET progress_pct=%s, log_tail=%s WHERE id=%s",
                    (pct, tail, task_id),
                )
            cn.close()
        except Exception as ex:
            logging.warning("progress update failed: " + str(ex)[:120])

    # VFORGE: manos en el repo y la rama del run, no en /root/vforge.
    _vf_cwd = _vforge_workspace(prompt, "claude")

    # -- 1. Claude Code REAL (streaming) --
    try:
        cwd = _vf_cwd or "/root/vforge"
        env = dict(_os.environ)
        env.pop("ANTHROPIC_API_KEY", None)   # forzar OAuth, no la API key muerta
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env["IS_SANDBOX"] = "1"              # permite bypassPermissions como root
        _perfil = _pick_claude_profile()
        if _perfil:
            env["CLAUDE_CONFIG_DIR"] = _perfil
            logging.info(f"pool: usando perfil B ({_perfil})")
        else:
            env.pop("CLAUDE_CONFIG_DIR", None)

        proc = _sp.Popen(
            ["claude", "-p", _inject_vulcano_boot(prompt),
             "--output-format", "stream-json", "--verbose",
             "--permission-mode", "bypassPermissions",
             "--max-turns", "120"],
            stdout=_sp.PIPE, stderr=_sp.PIPE, text=True,
            cwd=cwd, env=env, stdin=_sp.DEVNULL, bufsize=1,
        )

        st = {"turns": 0, "tail": _deque(maxlen=5), "result": None,
              "done": False, "timed_out": False}
        lock = _th.Lock()
        errbuf = []
        deadline = _t2.time() + _claude_timeout()

        def _tgt(inp):
            # objetivo legible de una herramienta (archivo / comando / patron)
            if not isinstance(inp, dict):
                return ""
            for k in ("file_path", "path", "notebook_path"):
                if inp.get(k):
                    return _os.path.basename(str(inp[k]))
            if inp.get("command"):
                return str(inp["command"]).replace(_NL, " ")[:60]
            if inp.get("pattern"):
                return str(inp["pattern"])[:50]
            if inp.get("url"):
                return str(inp["url"])[:60]
            if inp.get("prompt"):
                return str(inp["prompt"]).replace(_NL, " ")[:50]
            return ""

        def _ingest(line):
            try:
                ev = _json.loads(line)
            except Exception:
                return
            et = ev.get("type")
            if et == "assistant":
                with lock:
                    st["turns"] += 1
                for blk in (ev.get("message") or {}).get("content", []) or []:
                    bt = blk.get("type")
                    if bt == "text":
                        txt = (blk.get("text") or "").strip().replace(_NL, " ")
                        if txt:
                            with lock:
                                st["tail"].append("• " + txt[:90])
                    elif bt == "tool_use":
                        nm = blk.get("name", "tool")
                        tg = _tgt(blk.get("input"))
                        with lock:
                            st["tail"].append("> " + nm + ((": " + tg) if tg else ""))
            elif et == "result":
                with lock:
                    st["result"] = ev.get("result")

        def _push(pct_cap=95):
            with lock:
                turns = st["turns"]
                tail = _NL.join(st["tail"])
            if not task_id:
                return
            pct = min(pct_cap, round(turns / EXPECTED_TURNS * 100)) if turns else 1
            _db_progress(pct, tail[-1400:])

        def _heartbeat():
            while True:
                _t2.sleep(15)
                with lock:
                    if st["done"]:
                        return
                _push()

        def _watchdog():
            while True:
                _t2.sleep(2)
                with lock:
                    if st["done"]:
                        return
                if _t2.time() > deadline:
                    with lock:
                        st["timed_out"] = True
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return

        def _drain_err():
            try:
                for ln in proc.stderr:
                    errbuf.append(ln)
            except Exception:
                pass

        _th.Thread(target=_heartbeat, daemon=True).start()
        _th.Thread(target=_watchdog, daemon=True).start()
        _th.Thread(target=_drain_err, daemon=True).start()

        for line in proc.stdout:
            line = line.strip()
            if line:
                _ingest(line)
        proc.wait()
        with lock:
            st["done"] = True
            result_text = st["result"]
            tail_final = _NL.join(st["tail"])
            timed_out = st["timed_out"]
        err = "".join(errbuf).strip()

        if timed_out:
            # Timeout REAL: propagar para que process() lo marque failed/timeout
            # (jamas falso positivo done=Timeout).
            raise _sp.TimeoutExpired(cmd="claude", timeout=_claude_timeout(), output=tail_final)

        out = (result_text or "").strip()
        if out and "Invalid API key" not in out and "Failed to authenticate" not in out and "401" not in (out + err)[:200]:
            if task_id:
                _db_progress(100, tail_final[-1400:])
            return out
        logging.warning("Claude Code auth issue, fallback a Claude B / Cerebras: " + (err or out)[:120])
    except _sp.TimeoutExpired:
        raise
    except Exception as e_cc:
        logging.warning("Claude Code failed: " + str(e_cc)[:120])

    # -- 2. Claude B fallback (segunda cuenta OAuth propia) --
    try:
        env_b = {**_os.environ, "HOME": "/root",
                 "CLAUDE_CONFIG_DIR": "/root/.claude-turbillon",
                 "IS_SANDBOX": "1", "DISABLE_AUTOUPDATER": "1"}
        env_b.pop("ANTHROPIC_API_KEY", None)
        env_b.pop("ANTHROPIC_AUTH_TOKEN", None)
        r_b = _sp.run(
            ["claude", "-p", _inject_vulcano_boot(prompt)[:12000], "--output-format", "text"],
            capture_output=True, text=True, timeout=90, env=env_b, cwd="/root"
        )
        out_b = (r_b.stdout or "").strip()
        if out_b and "Invalid API key" not in out_b and "Failed to authenticate" not in out_b:
            logging.info("Claude B fallback OK")
            return out_b
        logging.warning("Claude B auth issue: " + out_b[:80])
    except Exception as e_b:
        logging.warning("Claude B fallback failed: " + str(e_b)[:120])

    # -- 3. Cerebras via mesh (obrero rapido, gpt-oss-120b) --
    try:
        import urllib.request as _ur, json as _jj
        _rk = _jj.load(open("/root/mesh-router/config.json"))["router_key"]
        payload = _jj.dumps({"model": "gpt-oss-120b", "max_tokens": 4000,
            "messages": [{"role": "user", "content": _inject_vulcano_boot(prompt)[:12000]}]}).encode()
        req = _ur.Request("http://127.0.0.1:8088/v1/chat/completions", data=payload,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + _rk})
        d = _jj.loads(_ur.urlopen(req, timeout=120).read())
        out_c = ((d.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
        if out_c:
            logging.info("Cerebras (mesh) fallback OK")
            return out_c
    except Exception as e_cb:
        logging.warning("Cerebras (mesh) fallback failed: " + str(e_cb)[:120])

    # -- 4. Ollama local (ultimo recurso) --
    try:
        import urllib.request as _ur, json as _jj
        payload = _jj.dumps({"model": "qwen2.5:1.5b", "prompt": prompt, "stream": False}).encode()
        req = _ur.Request("http://127.0.0.1:11434/api/generate",
            data=payload, headers={"Content-Type": "application/json"})
        d = _jj.loads(_ur.urlopen(req, timeout=_current_timeout()).read())
        return d.get("response", "")
    except Exception as e_ol:
        return "ERROR: todos los backends fallaron. Claude Code necesita re-login (claude setup-token). Last: " + str(e_ol)[:100]

def run_shell(prompt: str) -> str:
    proc = subprocess.run(
        prompt, shell=True, capture_output=True, text=True, timeout=_current_timeout()
    )
    out = proc.stdout.strip()
    err = proc.stderr.strip()
    if proc.returncode != 0:
        raise RuntimeError(f"shell exit {proc.returncode}: {err[:500]}")
    return out if out else err

def run_browser(prompt: str) -> str:
    """
    Executes browser tasks via Playwright (headless Chromium) with:
    - playwright-stealth: bypass bot detection / reCAPTCHA v3
    - Capsolver: solve reCAPTCHA v2, hCaptcha, Cloudflare Turnstile

    prompt JSON keys:
      url         str   — target URL
      actions     list  — steps to execute
      capsolver_key str — optional override (uses env CAPSOLVER_API_KEY by default)

    Action types:
      screenshot, click, fill, wait, text, eval, navigate,
      solve_captcha (type: recaptcha2|recaptcha3|hcaptcha|turnstile, site_key, page_url)
    """
    import time as _t
    from playwright.sync_api import sync_playwright

    # Stealth import (soft — don't crash if not installed)
    try:
        from playwright_stealth import stealth_sync
        HAS_STEALTH = True
    except ImportError:
        HAS_STEALTH = False

    # Capsolver import
    try:
        import capsolver as _capsolver
        HAS_CAPSOLVER = True
    except ImportError:
        HAS_CAPSOLVER = False

    CAPSOLVER_KEY = os.environ.get("CAPSOLVER_API_KEY", "")

    try:
        task = json.loads(prompt)
    except json.JSONDecodeError:
        task = {"url": prompt, "actions": [{"type": "screenshot"}]}

    url     = task.get("url", "about:blank")
    actions = task.get("actions", [{"type": "screenshot"}])
    cap_key = task.get("capsolver_key", CAPSOLVER_KEY)
    results = []

    # === MODO VISIBLE: usar el Chrome del contenedor vulcano-browser (visible por VNC) ===
    # Si el task pide "visible": true, controlamos el browser que Luis VE en el VNC,
    # via el controlador CDP (cdp_control.py) dentro del contenedor. Asi la IA navega
    # y Luis lo mira en tiempo real en https://vulcano.vmomentum.site
    if task.get("visible"):
        import subprocess as _sp
        import urllib.request as _ur
        def _evento(tipo, msg):
            # registra la accion en el log "Vulcano conduciendo" (handoff_api)
            try:
                data = json.dumps({"type": tipo, "msg": msg}).encode()
                req = _ur.Request("http://127.0.0.1:6090/api/handoff/event", data=data, method="POST")
                _ur.urlopen(req, timeout=5)
            except Exception:
                pass
        def _ctrl(payload):
            j = json.dumps(payload)
            try:
                out = _sp.run(
                    ["docker", "exec", "vulcano-browser", "python3",
                     "/home/kasm-user/cdp_control.py", j],
                    capture_output=True, text=True, timeout=40
                ).stdout.strip()
                res = json.loads(out) if out else {"ok": False, "error": "sin salida"}
                # registrar evento legible segun la accion
                a = payload.get("action")
                if a == "navigate":
                    _evento("ok", "Vulcano navego a " + payload.get("url", ""))
                elif a == "type":
                    _evento("ok", "Vulcano escribio en " + payload.get("selector", "campo"))
                elif a == "click":
                    _evento("ok", "Vulcano hizo click en " + payload.get("selector", "elemento"))
                elif a == "eval" or a == "read":
                    _evento("ok", "Vulcano leyo contenido de la pagina")
                return res
            except Exception as e:
                return {"ok": False, "error": str(e)}
        vis_results = []
        if url and url != "about:blank":
            vis_results.append(_ctrl({"action": "navigate", "url": url}))
            _t.sleep(2)
        for act in actions:
            t = act.get("type", "")
            if t == "navigate":
                vis_results.append(_ctrl({"action": "navigate", "url": act.get("url", url)})); _t.sleep(2)
            elif t == "click":
                vis_results.append(_ctrl({"action": "click", "selector": act.get("selector", "")})); _t.sleep(1)
            elif t == "fill" or t == "type":
                vis_results.append(_ctrl({"action": "type", "selector": act.get("selector", ""), "text": act.get("value", act.get("text", ""))})); _t.sleep(1)
            elif t == "eval":
                vis_results.append(_ctrl({"action": "eval", "expr": act.get("expr", "document.title")}))
            elif t == "text" or t == "read":
                vis_results.append(_ctrl({"action": "eval", "expr": "document.body ? document.body.innerText.slice(0,3000) : ''"}))
            elif t == "wait":
                _t.sleep(float(act.get("seconds", 1)))
            else:
                vis_results.append({"type": t, "skipped": "no soportado en modo visible"})
        # estado final
        tabs = _ctrl({"action": "tabs"})
        return json.dumps({"visible": True, "results": vis_results, "tabs": tabs})

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        # Apply stealth patches
        if HAS_STEALTH:
            stealth_sync(page)

        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        for act in actions:
            t = act.get("type", "")
            try:
                if t == "screenshot":
                    sc_dir  = "/root/agents/screenshots"
                    sc_path = f"{sc_dir}/sc_{int(_t.time())}.png"
                    page.screenshot(path=sc_path, full_page=act.get("full_page", False))
                    results.append({"type": "screenshot", "path": sc_path})

                elif t == "click":
                    page.click(act["selector"], timeout=10000)
                    results.append({"type": "click", "selector": act["selector"], "ok": True})

                elif t == "fill":
                    page.fill(act["selector"], act.get("value", ""), timeout=10000)
                    results.append({"type": "fill", "selector": act["selector"], "ok": True})

                elif t == "wait":
                    page.wait_for_timeout(int(act.get("ms", 1000)))
                    results.append({"type": "wait", "ms": act.get("ms", 1000)})

                elif t == "text":
                    el   = page.query_selector(act["selector"])
                    text = el.inner_text() if el else None
                    results.append({"type": "text", "selector": act["selector"], "value": text})

                elif t == "eval":
                    val = page.evaluate(act["js"])
                    results.append({"type": "eval", "js": act["js"], "value": str(val)})

                elif t == "navigate":
                    page.goto(act["url"], wait_until="domcontentloaded", timeout=30000)
                    results.append({"type": "navigate", "url": act["url"], "ok": True})

                elif t == "solve_captcha":
                    if not HAS_CAPSOLVER:
                        results.append({"type": "solve_captcha", "error": "capsolver not installed"})
                        continue
                    if not cap_key:
                        results.append({"type": "solve_captcha", "error": "no CAPSOLVER_API_KEY"})
                        continue

                    _capsolver.api_key = cap_key
                    cap_type  = act.get("captcha_type", "recaptcha2")
                    site_key  = act.get("site_key", "")
                    page_url  = act.get("page_url", page.url)

                    if cap_type == "recaptcha2":
                        sol = _capsolver.solve({
                            "type": "ReCaptchaV2Task",
                            "websiteURL": page_url,
                            "websiteKey": site_key,
                        })
                        token = sol["solution"]["gRecaptchaResponse"]
                        # Inject token into page
                        page.evaluate(f"document.getElementById('g-recaptcha-response').innerHTML='{token}'")

                    elif cap_type == "recaptcha3":
                        sol = _capsolver.solve({
                            "type": "ReCaptchaV3Task",
                            "websiteURL": page_url,
                            "websiteKey": site_key,
                            "pageAction": act.get("action", "submit"),
                        })
                        token = sol["solution"]["gRecaptchaResponse"]
                        page.evaluate(f"document.getElementById('g-recaptcha-response').innerHTML='{token}'")

                    elif cap_type == "hcaptcha":
                        sol = _capsolver.solve({
                            "type": "HCaptchaTask",
                            "websiteURL": page_url,
                            "websiteKey": site_key,
                        })
                        token = sol["solution"]["gRecaptchaResponse"]
                        page.evaluate(f"document.querySelector('[name=h-captcha-response]').innerHTML='{token}'")

                    elif cap_type == "turnstile":
                        sol = _capsolver.solve({
                            "type": "AntiTurnstileTaskProxyLess",
                            "websiteURL": page_url,
                            "websiteKey": site_key,
                        })
                        token = sol["solution"]["token"]
                        page.evaluate(f"document.querySelector('[name=cf-turnstile-response]').value='{token}'")

                    results.append({"type": "solve_captcha", "captcha_type": cap_type, "ok": True})

                else:
                    results.append({"type": t, "error": "unknown action type"})

            except Exception as e:
                results.append({"type": t, "error": str(e)})

        title     = page.title()
        final_url = page.url
        browser.close()

    return json.dumps({
        "url": final_url,
        "title": title,
        "stealth": HAS_STEALTH,
        "capsolver": HAS_CAPSOLVER,
        "actions_done": len(results),
        "results": results
    })


# ── run_grok: reviewer, no editor ─────────────────────────────────────────────

def run_grok_chat(prompt: str) -> str:
    """Grok como cerebro conversacional — sin wrapper de auditor.
    Usa la cuenta SuperGrok de Luis via CLI. Costo adicional: $0."""
    import subprocess as _sp, os as _os, re as _re
    env = {**_os.environ, "HOME": "/root",
           "PATH": "/root/.nvm/versions/node/v20.20.2/bin:" + _os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    try:
        grok_bin = "/usr/local/bin/grok" if _os.path.exists("/usr/local/bin/grok") else "grok"
        proc = _sp.run(
            [grok_bin, "-p", prompt[:8000], "--output-format", "plain", "--permission-mode", "dontAsk", "--no-subagents"],
            capture_output=True, text=True, timeout=180, env=env, cwd="/tmp"
        )
        if proc.returncode and not (proc.stdout or "").strip():
            return ("grok exit %s\n" % proc.returncode) + ((proc.stderr or proc.stdout or "")[:2000])
        raw = (proc.stdout or "").strip()
        # Limpiar noise del CLI (warnings ANSI, AuthorizationRequired)
        lines = [l for l in raw.splitlines()
                 if "AuthorizationRequired" not in l
                 and "worker quit" not in l
                 and not l.startswith("\033[")]
        result = "\n".join(lines).strip()
        return result if result else "Grok no respondio"
    except Exception as e:
        return "grok_chat error: " + str(e)[:200]

def run_grok(prompt: str) -> str:
    """Auditor Grok (reviewer, no editor). Pasa el trabajo INLINE en el prompt —
    NO le pide leer un archivo: la herramienta de lectura del grok CLI exige
    Auth(AuthorizationRequired) y devolvia vacio (=> siempre REVISION). Inline el
    agente principal responde directo con el veredicto."""
    import subprocess as _sp, os as _os, json as _jj
    env = {**_os.environ, "HOME": "/root",
           "PATH": "/root/.nvm/versions/node/v20.20.2/bin:" + _os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    instruction = (
        "Eres un auditor de calidad de software. Audita el siguiente trabajo y "
        "responde EMPEZANDO con una sola palabra en mayusculas: APROBADO o "
        "RECHAZADO, seguida de notas breves que justifiquen. No uses herramientas.\n\n"
        "=== TRABAJO A AUDITAR ===\n" + (prompt or "")
    )
    try:
        proc = _sp.run(
            ["grok", "-p", instruction],
            capture_output=True, text=True, timeout=120, env=env, cwd="/tmp"
        )
        raw = (proc.stdout or "").strip()
        # Descarta lineas de log del runtime (ERROR/WARN/AuthorizationRequired, ANSI).
        lines = [l for l in raw.splitlines()
                 if "AuthorizationRequired" not in l and "worker quit" not in l]
        clean = "\n".join(lines).strip() or (proc.stderr or "").strip()
        up = clean.upper()
        verdict = "APROBADO" if "APROBADO" in up else "RECHAZADO" if "RECHAZADO" in up else "REVISION"
        return _jj.dumps({"verdict": verdict, "notes": clean[:2000]})
    except Exception as e:
        return _jj.dumps({"verdict": "REVISION", "notes": "grok error: " + str(e)[:300]})

# ── _update_task: visibilidad en dispatch_queue ────────────────────────────────
def _update_task(task_id: int, fields: dict):
    import urllib.request as _ur, json as _jj
    try:
        sets, vals = [], []
        for k, v in fields.items():
            vals.append(v)
            sets.append(f"{k}=${len(vals)}")
        vals.append(task_id)
        body = _jj.dumps({
            "secret": "superclaude2025",
            "query": f"UPDATE dispatch_queue SET {','.join(sets)} WHERE id=${len(vals)}",
            "params": vals
        }).encode()
        req = _ur.Request("http://127.0.0.1:9000/brain/query",
                          data=body, headers={"Content-Type": "application/json"})
        _ur.urlopen(req, timeout=5)
    except Exception as ex:
        logging.warning(f"_update_task failed: {ex}")

# ── run_codex: ejecutor en worktree aislado ────────────────────────────────────
def run_codex(prompt: str) -> str:
    import subprocess as _sp, os as _os, json as _jj, time as _t3, re as _re
    env = {**_os.environ, "HOME": "/root"}
    try:
        cfg = _jj.loads(prompt)
    except Exception:
        repo_m = _re.search(r"^REPOSITORIO\s+([^\s]+)", prompt, _re.M)
        branch_m = _re.search(r"^RAMA DE TRABAJO\s+([^\s]+)", prompt, _re.M)
        run_m = _re.search(r"^VFORGE RUN\s+([^\s]+)", prompt, _re.M)
        if repo_m and branch_m:
            cfg = {
                "task": prompt,
                "repo": repo_m.group(1).strip(),
                "branch": branch_m.group(1).strip(),
                "gajo": (run_m.group(1)[:8] if run_m else "VFORGE"),
                "vforge_remote": True,
            }
        else:
            cfg = {"task": prompt, "repo": "/root", "gajo": "X"}
    repo     = cfg.get("repo", "/root")
    gajo     = cfg.get("gajo", "X")
    branch   = cfg.get("branch", f"gajo-{str(gajo).lower()}-{int(_t3.time())}")
    task     = cfg.get("task", "")
    task_id  = cfg.get("task_id") or _current_task_id()
    is_remote_vforge = bool(cfg.get("vforge_remote")) and isinstance(repo, str) and "/" in repo
    worktree = (
        f"/tmp/vforge-codex-{task_id or int(_t3.time())}"
        if is_remote_vforge
        else f"/tmp/worktree-{branch}"
    )
    log.info(f"[Codex] Gajo {gajo} repo={repo} branch={branch} worktree={worktree}")
    try:
        if is_remote_vforge:
            clone = _sp.run(
                ["gh", "repo", "clone", repo, worktree, "--", "--branch", branch, "--single-branch"],
                capture_output=True, text=True, env=env,
            )
            if clone.returncode != 0:
                raise RuntimeError((clone.stderr or clone.stdout or "gh repo clone failed")[-1000:])
        else:
            _sp.run(["git", "-C", repo, "worktree", "add", "-b", branch, worktree],
                    capture_output=True, check=True, env=env)
        if task_id:
            _update_task(task_id, {"worktree_path": worktree, "gajo": gajo, "progress_pct": 10,
                                   "log_tail": f"workspace creado: {worktree}"})
        proc = _sp.run(
            ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", "-"],
            input=task, capture_output=True, text=True, timeout=_current_timeout(),
            cwd=worktree, env=env
        )
        output = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode != 0:
            raise RuntimeError(f"codex exit {proc.returncode}: {output[-1200:]}")
        status = _sp.run(["git", "-C", worktree, "status", "--short"],
                         capture_output=True, text=True, env=env)
        files = []
        for line in status.stdout.splitlines():
            name = line[3:].strip() if len(line) > 3 else line.strip()
            if name:
                files.append(name)
        if task_id:
            _update_task(task_id, {"files_touched": files, "progress_pct": 85,
                                   "log_tail": output[-400:]})
        return _jj.dumps({"gajo": gajo, "branch": branch, "worktree": worktree,
                          "files_touched": files, "output": output[:3000]})
    except Exception as e:
        log.error(f"[Codex] Gajo {gajo} error: {e}")
        if task_id: _update_task(task_id, {"log_tail": f"ERROR: {str(e)}", "progress_pct": 0})
        raise


def run_browser_nl(prompt: str) -> str:
    """Navegador de lenguaje natural: descompone la meta con Claude Code (Max OAuth)
    y la ejecuta en el Chrome VISIBLE del contenedor. El prompt es la meta en texto."""
    import subprocess as _sp
    try:
        out = _sp.run(
            ["python3", "/root/agents/nl_browser_agent.py", prompt],
            capture_output=True, text=True, timeout=240
        )
        return (out.stdout or out.stderr or "").strip() or '{"ok": false, "error": "sin salida"}'
    except _sp.TimeoutExpired:
        # Timeout REAL: propagar para que process() lo marque failed/timeout.
        raise
    except Exception as e:
        return '{"ok": false, "error": "' + str(e)[:100] + '"}'


def run_mesh(prompt: str) -> str:
    """Genera con el mesh router (GPU propia qwen3.5 / Cerebras). Cero tokens Claude."""
    import json as _j, urllib.request as _u
    cfg = _j.load(open('/root/mesh-router/config.json'))
    body = _j.dumps({
        "policy": "auto",
        "messages": [
            {"role": "system", "content": "Eres un generador tecnico. Entrega SOLO el resultado pedido, sin preambulos ni cierres."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 8000
    }).encode()
    req = _u.Request('http://127.0.0.1:8088/v1/chat/completions', data=body,
                     headers={'Content-Type': 'application/json',
                              'Authorization': 'Bearer ' + cfg['router_key']})
    with _u.urlopen(req, timeout=600) as r:
        d = _j.loads(r.read().decode())
    try:
        return d['choices'][0]['message']['content']
    except Exception:
        return _j.dumps(d)[:4000]

def run_glm(prompt: str) -> str:
    """Runner GLM: Claude Code CLI apuntando a Z.ai. Cuentas A/B en /etc/vl-secrets/glm.env."""
    import subprocess as _sp
    try:
        r = _sp.run(["/usr/local/bin/glm-code", "-p", prompt, "--dangerously-skip-permissions"],
                    capture_output=True, text=True, timeout=7200)
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if r.returncode != 0 and not out:
            return "rc=%d: %s" % (r.returncode, err[:500])
        return out or err or "done"
    except _sp.TimeoutExpired:
        return "Timeout"
    except Exception as _e:
        return str(_e)


EXECUTORS = {
    "mesh":    run_mesh,
    "claude":  run_codex,  # FRENTE 2026-10: OAuth Claude sin suscripcion; Codex corre los builds
    "shell":   run_shell,
    "browser": run_browser,
    "browser_nl": run_browser_nl,
    "grok":    run_grok_chat,
    "grok_audit": run_grok,
    "codex":   run_codex,
    "glm":     run_glm,
}

# ── DB helpers ─────────────────────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)


# ── REPORTE OBLIGATORIO AL BRAIN (antes de cualquier mark done) ──────
def _forced_brain_report(task_id, task_type, source, prompt, result):
    """
    Extrae el estado real de la tarea y lo sube al Brain obligatoriamente.
    Sin esto, el job NO se puede marcar done. Es el contrato de cierre.
    """
    import urllib.request as _ur2, json as _json2, re as _re2

    # Detectar proyecto desde source o prompt
    proyecto = source or "general"
    # Limpiar source para nombre de archivo
    safe = _re2.sub(r"[^a-z0-9-]", "-", proyecto.lower())[:40]

    # Extraer resumen del resultado (primeras 400 chars con sentido)
    resumen = (result or "")[:600].strip()
    if not resumen:
        resumen = f"Task #{task_id} completada sin output textual."

    # Detectar si hay pendientes explícitos en el resultado
    pendientes = []
    for linea in (result or "").split("\n"):
        l = linea.strip().lower()
        if any(x in l for x in ["pendiente", "falta", "TODO", "next", "siguiente", "missing", "❌"]):
            pendientes.append(linea.strip()[:120])

    contenido = f"""# Reporte automático — {proyecto}
**Task:** #{task_id} · agent={task_type} · source={source}
**Fecha:** {__import__("time").strftime("%Y-%m-%d %H:%M UTC", __import__("time").gmtime())}

## Qué se hizo
{resumen}

## Pendientes detectados
{chr(10).join("- " + p for p in pendientes[:8]) if pendientes else "_(ninguno detectado en el output)_"}

## Prompt original (resumen)
{(prompt or "")[:200]}
"""
    file_path = f"projects/status-{safe}-{task_id}.md"
    try:
        payload = _json2.dumps({
            "secret": "superclaude2025",
            "file": file_path,
            "content": contenido
        }).encode()
        req = _ur2.Request(
            "http://127.0.0.1:9000/brain/save",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        r = _json2.loads(_ur2.urlopen(req, timeout=15).read())
        log.info(f"[{task_id}] brain-report → {file_path} ok={r.get('ok')}")
        return True
    except Exception as e:
        log.warning(f"[{task_id}] brain-report FALLÓ: {e} — job se cierra igual (no bloquear)")
        return False

def fetch_pending(cur):
    cur.execute("""
        SELECT * FROM dispatch_queue
        WHERE status = 'pending'
        ORDER BY created_at
        LIMIT 5
    """)
    return cur.fetchall()

def mark(cur, task_id, status, result=None, error=None, log_tail=None):
    cur.execute("""
        UPDATE dispatch_queue
        SET status=%s, result=%s, error=%s,
            log_tail = COALESCE(%s, log_tail),
            started_at  = CASE WHEN %s='running'            THEN now() ELSE started_at  END,
            completed_at = CASE WHEN %s IN ('done','failed') THEN now() ELSE completed_at END
        WHERE id=%s
    """, (status, result, error, log_tail, status, status, task_id))

# ── Main loop ──────────────────────────────────────────────────────────────


def _enqueue_subtasks(result: str, parent_id: int, cur):
    import re
    pattern = re.compile(r'\[SUBTASK:\s*(.+?)\s*\|\s*(shell|claude|browser|browser_nl|grok|codex)\s*\]', re.I)
    for m in pattern.finditer(result):
        prompt_s, agent_s = m.group(1).strip(), m.group(2).lower()
        try:
            cur.execute(
                "INSERT INTO dispatch_queue (prompt,agent,priority,source,parent_task_id) VALUES (%s,%s,%s,%s,%s)",
                (prompt_s, agent_s, 7, f"subtask:{parent_id}", parent_id)
            )
            log.info(f"[subtask] enqueued [{agent_s}]: {prompt_s[:50]}")
        except Exception as ex:
            log.warning(f"[subtask] enqueue failed: {ex}")

def _update_grok_verdict(task_id: int, result_json: str, cur, parent_id=None):
    """Persiste el veredicto Grok. Escribe en el PROPIO job grok (recencia para el
    lastVerdict del Taller) y, si fue una auditoria, TAMBIEN en el job claude
    auditado (parent_id) — ahi es donde el Taller pinta el badge del veredicto y
    donde antes grok_verdict quedaba None en todos los jobs claude."""
    import json as _jj
    try:
        d = _jj.loads(result_json)
        verdict = d.get("verdict", "REVISION")
        notes   = (d.get("notes") or "")[:500]
    except Exception:
        # run_grok devuelve JSON; si por algo no, derivar del texto crudo.
        txt = result_json or ""
        up = txt.upper()
        verdict = "APROBADO" if "APROBADO" in up else "RECHAZADO" if "RECHAZADO" in up else "REVISION"
        notes = txt[:500]
    try:
        cur.execute(
            "UPDATE dispatch_queue SET grok_verdict=%s, grok_notes=%s WHERE id=%s",
            (verdict, notes, task_id)
        )
        if parent_id:
            cur.execute(
                "UPDATE dispatch_queue SET grok_verdict=%s, grok_notes=%s WHERE id=%s",
                (verdict, notes, parent_id)
            )
            log.info(f"[grok-audit] veredicto {verdict} persistido en job claude #{parent_id}")
    except Exception as ex:
        log.warning(f"[grok-audit] persist failed: {ex}")


def enqueue_grok_audit(task: dict, result: str, cur):
    """GROK AUDITOR CABLEADO: al completar un job CLAUDE, despacha automaticamente
    un job grok que AUDITA el resultado. El job grok hereda gajo/source/parent del
    claude => en el Taller la esfera de Grok se ENCIENDE con el mismo proyecto
    mientras audita, y al terminar el veredicto se persiste en grok_verdict/
    grok_notes del job claude (via _update_grok_verdict con parent_id)."""
    try:
        parent_id = task["id"]
        gajo   = task.get("gajo")
        source = task.get("source") or ""
        # No re-auditar una auditoria (defensa extra; los jobs grok no llaman aqui).
        if str(source).startswith("audit:"):
            return None
        prompt_txt = (task.get("prompt") or "")[:1500]
        audit_prompt = (
            f"AUDITORIA DE TRABAJO — job claude #{parent_id}.\n\n"
            f"TAREA ORIGINAL:\n{prompt_txt}\n\n"
            f"RESULTADO ENTREGADO POR CLAUDE:\n{(result or '')[:4000]}\n\n"
            "Da VEREDICTO claro: 'APROBADO' si el trabajo cumple la tarea sin inventos "
            "ni demos vacias y es real; 'RECHAZADO' si no cumple. Agrega notas breves."
        )
        # source: conserva el del padre para agrupar por proyecto; marca audit en gajo-fallback.
        audit_source = source or f"audit:{parent_id}"
        cur.execute(
            "INSERT INTO dispatch_queue (prompt, agent, priority, source, parent_task_id, gajo) "
            "VALUES (%s,'grok',%s,%s,%s,%s) RETURNING id",
            (audit_prompt, 6, audit_source, parent_id, gajo)
        )
        new_id = cur.fetchone()["id"]
        log.info(f"[grok-audit] job claude #{parent_id} done -> auditoria grok #{new_id}")
        return new_id
    except Exception as ex:
        log.warning(f"[grok-audit] enqueue failed: {ex}")
        return None


# ══════════════════════════════════════════════════════════════
# LEARNING LOOP — Cierra el ciclo: tarea exitosa → patrón → vector
# Cada job APROBADO se convierte en conocimiento indexado
# ══════════════════════════════════════════════════════════════
def learning_loop(task_id, task_type, prompt, result, verdict="APROBADO"):
    """Extrae patrón de un job exitoso y lo vectoriza en embeddings."""
    if verdict not in ("APROBADO", "done"):
        return
    try:
        import urllib.request as _ur, json as _j
        # 1. Construir chunk de conocimiento
        chunk = (
            f"TAREA [{task_type}] #{task_id}\n"
            f"PROMPT: {str(prompt)[:300]}\n"
            f"RESULTADO: {str(result)[:600]}\n"
            f"VEREDICTO: {verdict}"
        )
        # 2. Guardar como patrón en Brain (via /brain/memory store)
        payload = _j.dumps({
            "secret": "superclaude2025",
            "action": "store",
            "agent": "learning_loop",
            "type": "pattern",
            "topic": f"task_{task_type}_{task_id}",
            "content": chunk,
            "importance": 7,
            "metadata": {
                "task_id": task_id,
                "task_type": task_type,
                "verdict": verdict,
                "source": "learning_loop"
            }
        }).encode()
        req = _ur.Request(
            "http://127.0.0.1:9000/brain/memory",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        _ur.urlopen(req, timeout=6)
        log.info(f"[learning_loop] task #{task_id} vectorizado en Brain")
    except Exception as ex:
        log.warning(f"[learning_loop] failed: {ex}")


# ══════════════════════════════════════════════════════════════
# KNOWLEDGE GRAPH — Relaciones entre conceptos del tenant
# ══════════════════════════════════════════════════════════════
def knowledge_graph_extract(prompt, result, task_type, tenant="vulcano"):
    """Extrae nodos/aristas de concepto del resultado y los persiste."""
    try:
        import urllib.request as _ur, json as _j, re as _re
        # Extraer conceptos clave (palabras capitalizadas / entidades)
        text = (prompt + " " + result)[:2000]
        # Simple heurística: palabras únicas de 4+ chars que aparecen 2+ veces
        words = _re.findall(r"\b[A-Za-záéíóúÁÉÍÓÚñÑ]{4,}\b", text)
        freq = {}
        for w in words:
            freq[w.lower()] = freq.get(w.lower(), 0) + 1
        concepts = [w for w, c in freq.items() if c >= 2 and w not in
                    {"para", "como", "este", "esta", "con", "que", "una",
                     "los", "las", "del", "por", "resultado", "tarea",
                     "task", "done", "error", "from", "import", "return"}][:8]
        if len(concepts) < 2:
            return
        # Guardar grafo como memoria semántica
        graph_entry = {
            "tenant": tenant,
            "task_type": task_type,
            "nodes": concepts,
            "edge": f"{concepts[0]} → {concepts[1]}",
            "context": text[:200]
        }
        payload = _j.dumps({
            "secret": "superclaude2025",
            "action": "store",
            "agent": "knowledge_graph",
            "type": "graph",
            "topic": f"graph_{tenant}_{concepts[0]}",
            "content": _j.dumps(graph_entry),
            "importance": 6
        }).encode()
        req = _ur.Request(
            "http://127.0.0.1:9000/brain/memory",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        _ur.urlopen(req, timeout=5)
        log.info(f"[knowledge_graph] nodos: {concepts[:4]} | tenant={tenant}")
    except Exception as ex:
        log.warning(f"[knowledge_graph] failed: {ex}")


# ══════════════════════════════════════════════════════════════
# SYNTHETIC STATE POR TENANT — Contexto maduro desde sus lecciones
# ══════════════════════════════════════════════════════════════
def synthetic_state_tenant(tenant="vulcano", task_type="CODE"):
    """Genera estado sintético personalizado desde las memorias del tenant."""
    try:
        import urllib.request as _ur, json as _j
        # Buscar memorias recientes del tenant en Brain
        payload = _j.dumps({
            "secret": "superclaude2025",
            "action": "search",
            "q": f"tenant {tenant} {task_type} pattern result",
            "limit": 5
        }).encode()
        # Primero intento semantic search
        try:
            req = _ur.Request(
                "http://127.0.0.1:9000/brain/search",
                data=_j.dumps({
                    "secret": "superclaude2025",
                    "q": f"{task_type} exitoso pattern lección",
                    "limit": 4,
                    "type": "pattern"
                }).encode(),
                headers={"Content-Type": "application/json"}
            )
            d = _j.loads(_ur.urlopen(req, timeout=6).read())
            hits = d.get("results", {}).get("memory", [])
        except Exception:
            hits = []

        if not hits:
            return ""

        lines = [f"[ESTADO SINTETICO TENANT={tenant} TASK={task_type}]"]
        for h in hits[:3]:
            lines.append(f"- {h.get('topic','')}: {str(h.get('content',''))[:120]}")
        lines.append("[FIN ESTADO SINTETICO]")
        state = "\n".join(lines)
        log.info(f"[synthetic_state_tenant] {len(hits)} hits para {tenant}/{task_type}")
        return state
    except Exception as ex:
        log.warning(f"[synthetic_state_tenant] failed: {ex}")
        return ""


def process(task, cur):
    _raw_agent = (task.get("agent") or task.get("task_type") or "claude").lower()
    # Smart detection: if marked as shell but looks like natural language, route to claude
    prompt = task.get("prompt", "")
    SHELL_HINT = re.compile(r'^(shell:|\$|ls |ps |pm2|cat |grep |echo |cd |mkdir |rm |curl |wget |python|node |uptime|free |df |which |systemctl|apt |pip )', re.I)
    if _raw_agent == "shell" and not SHELL_HINT.match(prompt.strip()):
        task_type = "claude"
        logging.info(f"[{task['id']}] NL detectado → redirigido a claude")
    else:
        task_type = _raw_agent
    prompt    = task.get("prompt") or task.get("payload") or ""
    retries   = task.get("retries") or 0
    task_id   = task["id"]

    # Timeout por job: usar metadata.timeout_secs si viene; si no, el TIMEOUT global.
    # THREAD-LOCAL (no global): con varios workers en paralelo cada uno tiene el suyo.
    _secs, _explicit = TIMEOUT, False
    md = task.get("metadata")
    if isinstance(md, str):
        try: md = json.loads(md)
        except Exception: md = None
    if isinstance(md, dict) and md.get("timeout_secs"):
        try:
            _ts = int(md["timeout_secs"])
            if _ts > 0:
                _secs, _explicit = _ts, True
        except Exception:
            _secs, _explicit = TIMEOUT, False
    _set_timeout(_secs, _explicit)

    executor = EXECUTORS.get(task_type)
    if not executor:
        mark(cur, task_id, "failed", error=f"unknown task_type: {task_type}")
        cur.connection.commit()
        return

    log.info(f"[{task_id}] type={task_type} retries={retries}")
    # El claim atomico (claim_one) ya marco running+started_at; no lo re-marcamos
    # para no pisar la marca de inicio real (importa para el % de ocupacion).

    try:
        _set_task_id(task_id)
        result = _ejecutar(executor, prompt, task_id, retries >= MAX_RETRY, task_type)
    except subprocess.TimeoutExpired as te:
        # TIMEOUT: SIEMPRE failed con error=timeout + log_tail del proceso.
        # NUNCA marcar done con result=Timeout (falso positivo job 64).
        tail = _timeout_tail(te)
        mark(cur, task_id, "failed",
             error=f"timeout tras {_current_timeout()}s", log_tail=tail or None)
        cur.connection.commit()
        log.error(f"[{task_id}] TIMEOUT tras {_current_timeout()}s")
        store_memory(
            agent="Vulcano", mtype="event", topic=task_type,
            content="Task #"+str(task_id)+" ["+task_type+"] TIMEOUT tras "+str(_current_timeout())+"s. "+str(prompt)[:80],
            importance=6
        )
        return
    except Exception as e:
        if retries < MAX_RETRY:
            # incrementa retries (la columna ya existe) para no reintentar infinito
            cur.execute(
                "UPDATE dispatch_queue SET status='pending', retries=COALESCE(retries,0)+1 WHERE id=%s",
                (task_id,)
            )
            log.warning(f"[{task_id}] retry {retries+1}: {e}")
        else:
            mark(cur, task_id, "failed", error=str(e)[:1000])
            log.error(f"[{task_id}] failed: {e}")
        cur.connection.commit()
        return

    # Si claude devolvio rate-limit, pausar el FEEDER 30 min (no martillar la cuenta).
    if task_type == "claude" and _looks_rate_limited(result):
        _pause_feeder(RATE_LIMIT_PAUSE_SECS, reason=f"job #{task_id} rate-limited")

    # Exito — REPORTE OBLIGATORIO AL BRAIN antes de cerrar
    _forced_brain_report(task_id, task_type,
                         task.get("source",""), prompt, result)
    mark(cur, task_id, "done", result=result[:16000])
    log.info(f"[{task_id}] done ({len(result)} chars)")
    store_memory(
        agent="Vulcano", mtype="event", topic=task_type,
        content="Task #"+str(task_id)+" ["+task_type+"] done. "+str(prompt)[:80]+" => "+str(result)[:120],
        importance=4
    )
    # ── QUANTUM UPGRADE: Learning Loop + Knowledge Graph ──────
    # Cada tarea exitosa se convierte en conocimiento vectorizado
    threading.Thread(
        target=learning_loop,
        args=(task_id, task_type, prompt, result, "APROBADO"),
        daemon=True
    ).start()
    threading.Thread(
        target=knowledge_graph_extract,
        args=(prompt, result, task_type, "vulcano"),
        daemon=True
    ).start()
    # ──────────────────────────────────────────────────────────
    # GROK AUDITOR: si este job fue una auditoria grok, persiste su veredicto en
    # el job claude auditado (parent_task_id) ademas del propio.
    if task_type == 'grok':
        _update_grok_verdict(task_id, result, cur, parent_id=task.get("parent_task_id"))
    # ...y si fue un job CLAUDE exitoso (no error), despacha su auditoria a Grok.
    elif task_type == 'claude' and not str(result).startswith("ERROR:"):
        pass  # auto-auditoria DESACTIVADA 02-sep-2026 por Luis: 61% REVISION, 174 jobs sin cliente
    # Auto-enqueue subtasks detected in result
    _enqueue_subtasks(result, parent_id=task_id, cur=cur)
    cur.connection.commit()

# ── Fragua Continua: guardarrailes + router + feeder + supervisor ───────────

# Pausa del feeder ante rate-limit de claude (no martillar la cuenta de Luis).
RATE_LIMIT_PAUSE_SECS = int(os.environ.get("VULCANO_RL_PAUSE", "1800"))  # 30 min
_feeder_lock = threading.Lock()
_feeder_pause_until = 0.0  # epoch secs

def _pause_feeder(secs, reason=""):
    global _feeder_pause_until
    with _feeder_lock:
        until = time.time() + secs
        if until > _feeder_pause_until:
            _feeder_pause_until = until
    log.warning(f"[feeder] PAUSA {secs}s — {reason}")
    store_memory(agent="Vulcano", mtype="event", topic="feeder",
                 content=f"Feeder pausado {secs}s: {reason}", importance=6)

def _feeder_paused():
    with _feeder_lock:
        return time.time() < _feeder_pause_until

_RL_PAT = re.compile(r'(rate.?limit|rate_limit|\b429\b|too many requests|usage limit|quota|overloaded|payment required|\b402\b)', re.I)
def _looks_rate_limited(text):
    return bool(text) and bool(_RL_PAT.search(text[:1500]))

# Router multi-agente: navegacion/web->browser, scripts/migraciones->shell,
# gajos de codigo->codex, builds completos->claude, veredictos->grok.
EXECUTORS["claude-encargo"] = lambda p: run_encargo(p, "claude")
VALID_AGENTS = set(EXECUTORS.keys())
def route_agent(agent, prompt):
    a = (agent or "").lower().strip()
    if a in VALID_AGENTS:
        return a
    p = (prompt or "").lower()
    if any(k in p for k in ("http://", "https://", "navega", "lighthouse", "screenshot", "verifica acceso", "abre la pagina")):
        return "browser"
    if any(k in p for k in ("veredicto", "aprobado", "rechazado", "revisa el codigo", "code review")):
        return "grok"
    if any(k in p for k in ("worktree", "gajo", "git diff")):
        return "codex"
    return "claude"  # builds completos

# Carga: no arrancar trabajo pesado si el load 1-min supera los nucleos.
def _overloaded():
    try:
        return os.getloadavg()[0] > (os.cpu_count() or 1)
    except Exception:
        return False

def claim_one(cur):
    """Reclama ATOMICAMENTE el pending de mayor prioridad (numero menor = primero).
    FOR UPDATE SKIP LOCKED => dos workers nunca toman el mismo job (mata el trabajo
    duplicado que sufria job 77)."""
    cur.execute("""
        UPDATE dispatch_queue SET status='running', started_at=now()
        WHERE id = (
            SELECT id FROM dispatch_queue
            WHERE status='pending'
            ORDER BY COALESCE(priority,5) ASC, created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING *
    """)
    row = cur.fetchone()
    cur.connection.commit()
    return row

def _pending_count(cur):
    cur.execute("SELECT count(*) AS n FROM dispatch_queue WHERE status='pending'")
    return cur.fetchone()["n"]

def feed_from_backlog(cur):
    """FEEDER: si NO hay pending directo y un worker quedo libre, inyecta UNA tarea
    del backlog (respetando intervalos de recurrentes). Serializado por _feeder_lock
    + check pending==0 para que dos workers no inyecten a la vez. Backlog entra con
    prioridad ALTA (numero grande) => SIEMPRE por debajo de los jobs directos de Luis."""
    if _feeder_paused() or _overloaded():
        return None
    with _feeder_lock:
        if _pending_count(cur) > 0:
            return None  # hay trabajo directo; el backlog espera
        cur.execute("""
            SELECT * FROM backlog_tasks
            WHERE activo = TRUE
              AND ( recurrente = FALSE
                 OR last_run IS NULL
                 OR now() - last_run >= make_interval(hours => COALESCE(intervalo_horas,24)) )
            ORDER BY (last_run IS NOT NULL), prioridad ASC,
                     COALESCE(last_run, '1970-01-01'::timestamptz) ASC, id ASC
            LIMIT 1
        """)
        bt = cur.fetchone()
        if not bt:
            return None
        agent = route_agent(bt["agent_sugerido"], bt["prompt"])
        prio = max(int(bt["prioridad"] or 100), 50)  # nunca por encima de los directos
        cur.execute(
            "INSERT INTO dispatch_queue (prompt, agent, priority, source) VALUES (%s,%s,%s,%s) RETURNING id",
            (bt["prompt"], agent, prio, f"backlog:{bt['id']}"))
        new_id = cur.fetchone()["id"]
        cur.execute("UPDATE backlog_tasks SET last_run=now() WHERE id=%s", (bt["id"],))
        if not bt["recurrente"]:
            cur.execute("UPDATE backlog_tasks SET activo=FALSE WHERE id=%s", (bt["id"],))
        cur.connection.commit()
        log.info(f"[feeder] backlog #{bt['id']} → dispatch #{new_id} [{agent}] prio={prio}: {bt['titulo']}")
        return new_id

def sweep_stale(cur):
    """SUPERVISOR: marca failed los running zombies (>STALE_HOURS sin terminar)."""
    hrs = int(os.environ.get("VULCANO_STALE_HOURS", "3"))
    cur.execute("""
        UPDATE dispatch_queue
        SET status='failed', error='stale: sin progreso (barrido supervisor)', completed_at=now()
        WHERE status='running' AND started_at < now() - make_interval(hours => %s)
        RETURNING id
    """, (hrs,))
    ids = [r["id"] for r in cur.fetchall()]
    cur.connection.commit()
    if ids:
        log.warning(f"[supervisor] stale → failed: {ids}")
    return ids

def worker_loop(wid):
    """Un worker: reclama → procesa → si no hay nada, alimenta del backlog → duerme."""
    log.info(f"[worker {wid}] arrancado")
    conn = get_conn()
    while not shutdown:
        try:
            with conn.cursor() as cur:
                if _overloaded():
                    log.info(f"[worker {wid}] load>{os.cpu_count()}, espero")
                    time.sleep(POLL_SECS * 2)
                    continue
                task = claim_one(cur)
                if task is None:
                    if feed_from_backlog(cur) is None:
                        time.sleep(POLL_SECS)
                    continue  # el job inyectado lo reclama el proximo ciclo
                process(task, cur)
        except Exception as e:
            log.error(f"[worker {wid}] loop error: {e}")
            try:
                conn.rollback()
            except Exception:
                try: conn.close()
                except Exception: pass
                conn = get_conn()
            time.sleep(POLL_SECS)
    try: conn.close()
    except Exception: pass
    log.info(f"[worker {wid}] detenido")

def main():
    log.info(f"Vulcano daemon v2 (fragua continua) — workers={WORKERS} executors=" + ", ".join(EXECUTORS.keys()))
    # Supervisor al arranque: limpia zombies de corridas anteriores.
    try:
        with get_conn() as c0:
            with c0.cursor() as cur0:
                sweep_stale(cur0)
    except Exception as e:
        log.error(f"sweep inicial: {e}")
    # Pool de workers concurrentes.
    threads = []
    for i in range(WORKERS):
        t = threading.Thread(target=worker_loop, args=(i + 1,), daemon=True)
        t.start(); threads.append(t)
    # Hilo supervisor: barrido de stale cada ~30 min (responde a shutdown).
    def _supervisor():
        while not shutdown:
            for _ in range(360):  # 360 * 5s = 30 min
                if shutdown: return
                time.sleep(5)
            try:
                with get_conn() as cs:
                    with cs.cursor() as curs:
                        sweep_stale(curs)
            except Exception as e:
                log.error(f"supervisor: {e}")
    threading.Thread(target=_supervisor, daemon=True).start()
    while not shutdown:
        time.sleep(1)
    log.info("Vulcano daemon stopped — esperando workers…")
    for t in threads:
        t.join(timeout=10)

if __name__ == "__main__":
    main()
