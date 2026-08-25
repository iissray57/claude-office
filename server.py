#!/usr/bin/env python3
"""Claude Office — 클로드 세션을 2D 픽셀 오피스로 보여주는 로컬 서버 (표준 라이브러리만 사용).

세션(리드)과 서브에이전트가 "지금 무슨 일을 하는지"를 보고 기능 팀(프론트/백엔드/테스트…)에 배정한다.
"""
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sys

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DEFAULT_CONFIG = {
    "projectsDir": str(Path.home() / ".claude" / "projects"),   # 클로드 세션 트랜스크립트 위치
    "managerName": "총괄 팀장",                                 # 사무실 총괄 팀장(본인) 이름
    "pmName": "PM",                                             # 관리동 PM석 이름
    "plName": "PL",                                             # 관리동 PL석 이름
    "port": 8765,
    "maxSessions": 12,
    "agentActiveSeconds": 120,
    "sessionMaxAgeDays": 7,
}
_config_lock = threading.Lock()


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.is_file():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
    except Exception as exc:
        print(f"⚠ config.json 읽기 실패, 기본값 사용: {exc}")
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


CONFIG = load_config()


def projects_dir():
    raw = (CONFIG.get("projectsDir") or DEFAULT_CONFIG["projectsDir"]).strip()
    return Path(os.path.expandvars(os.path.expanduser(raw)))


PORT = int(CONFIG.get("port") or 8765)
TAIL_BYTES = 300_000        # 대용량 트랜스크립트는 꼬리만 읽는다
MAX_AGENTS_PER_SESSION = 6
MIN_SIZE_BYTES = 5 * 1024
CACHE_TTL = 2.0
AGENT_TAIL_BYTES = 80_000
RECENT_TOOLS = 12           # 팀 판정에 쓰는 최근 tool_use 개수

_cache = {"at": 0.0, "payload": None}
_cache_lock = threading.Lock()

# ───────────────────────── 팀 정의 (클라이언트와 공유) ─────────────────────────
TEAMS = [
    {"key": "pm",       "name": "PM팀",          "icon": "📋"},
    {"key": "pl",       "name": "PL·아키텍트팀", "icon": "📐"},
    {"key": "frontend", "name": "프론트엔드팀",  "icon": "🎨"},
    {"key": "backend",  "name": "백엔드팀",      "icon": "⚙️"},
    {"key": "data",     "name": "데이터·분석팀", "icon": "📊"},
    {"key": "test",     "name": "테스트·QA팀",   "icon": "🧪"},
    {"key": "review",   "name": "리뷰·보안팀",   "icon": "🔍"},
    {"key": "debug",    "name": "디버깅팀",      "icon": "🐛"},
    {"key": "devops",   "name": "빌드·배포팀",   "icon": "🚀"},
    {"key": "research", "name": "리서치팀",      "icon": "🔎"},
    {"key": "docs",     "name": "문서팀",        "icon": "📝"},
    {"key": "general",  "name": "종합개발팀",    "icon": "💼"},
    {"key": "lounge",   "name": "휴게실",        "icon": "☕"},
]
TEAM_KEYS = {t["key"] for t in TEAMS}

# 서브에이전트 타입 → 팀
AGENT_TYPE_TEAM = {
    "explore": "research", "document-specialist": "research", "researcher": "research",
    "dependency-expert": "research", "claude-code-guide": "research",
    "planner": "pm", "analyst": "pm", "product-manager": "pm", "information-architect": "pm",
    "ux-researcher": "pm", "product-analyst": "pm",
    "architect": "pl", "critic": "pl", "plan": "pl",
    "designer": "frontend",
    "test-engineer": "test", "tdd-guide": "test", "qa-tester": "test", "quality-strategist": "test",
    "verifier": "test",
    "code-reviewer": "review", "security-reviewer": "review", "quality-reviewer": "review",
    "style-reviewer": "review", "api-reviewer": "review", "performance-reviewer": "review",
    "code-simplifier": "review",
    "debugger": "debug", "tracer": "debug",
    "build-fixer": "devops", "git-master": "devops",
    "writer": "docs",
    "scientist": "data", "vision": "data",
}
# 에이전트 이름/설명 키워드 → 팀 (커스텀 팀메이트 이름: backend-builder, qa-flow …)
NAME_KEYWORDS = [
    ("test", [r"\bqa\b", r"test", r"e2e", r"테스트", r"검증", r"verify"]),
    ("frontend", [r"front", r"\bui\b", r"\bweb\b", r"screen", r"page", r"화면", r"컴포넌트", r"design", r"css", r"style"]),
    ("backend", [r"back", r"\bapi\b", r"server", r"\bdb\b", r"database", r"서버", r"백엔드", r"schema", r"migration"]),
    ("review", [r"review", r"security", r"리뷰", r"보안", r"audit"]),
    ("debug", [r"debug", r"fix\b", r"bug", r"디버", r"버그", r"trace"]),
    ("devops", [r"build", r"deploy", r"\bci\b", r"docker", r"배포", r"빌드", r"release"]),
    ("docs", [r"doc", r"readme", r"문서", r"guide"]),
    ("research", [r"explore", r"search", r"research", r"탐색", r"조사", r"분석"]),
    ("pm", [r"plan", r"prd", r"requirement", r"기획", r"요구사항", r"일정"]),
    ("pl", [r"arch", r"design-spec", r"설계", r"구조", r"critic"]),
    ("data", [r"data", r"analy", r"stat", r"데이터", r"통계", r"chart"]),
]

FRONT_EXT = {".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss", ".sass", ".less", ".html", ".astro", ".mjs"}
BACK_EXT = {".py", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".sql", ".prisma", ".proto", ".scala", ".ex", ".exs"}
DOC_EXT = {".md", ".mdx", ".rst", ".txt", ".adoc"}
DATA_EXT = {".ipynb", ".csv", ".parquet", ".xlsx", ".r"}
DEVOPS_FILES = {"dockerfile", "docker-compose.yml", "docker-compose.yaml", "makefile", "package.json",
                "pnpm-lock.yaml", "package-lock.json", "yarn.lock", "tsconfig.json", "vite.config.ts",
                "webpack.config.js", "requirements.txt", "pyproject.toml", "cargo.toml", "go.mod", ".env"}
FRONT_DIRS = ("/components/", "/pages/", "/app/", "/views/", "/ui/", "/frontend/", "/web/", "/client/",
              "/public/", "/styles/", "/layouts/", "/hooks/", "/screens/", "/widgets/")
BACK_DIRS = ("/api/", "/server/", "/backend/", "/services/", "/routes/", "/controllers/", "/models/",
             "/db/", "/migrations/", "/handlers/", "/repository/", "/domain/", "/internal/", "/cmd/")
TEST_DIRS = ("/test/", "/tests/", "/__tests__/", "/e2e/", "/cypress/", "/spec/", "/playwright/")
DEVOPS_DIRS = ("/.github/", "/.gitlab/", "/k8s/", "/helm/", "/terraform/", "/infra/", "/deploy/", "/docker/", "/ci/")
DOC_DIRS = ("/docs/", "/doc/", "/documentation/")


def _classify_path(fp):
    """파일 경로 → (팀, 확신도)."""
    if not fp:
        return None
    low = fp.lower().replace("\\", "/")
    base = os.path.basename(low)
    ext = os.path.splitext(base)[1]
    if any(d in low for d in TEST_DIRS) or re.search(r"\.(test|spec)\.[a-z]+$", base) or base.startswith("test_") \
            or base.endswith("_test.go") or base.endswith("_test.py"):
        return ("test", 1.0)
    if base in DEVOPS_FILES or any(d in low for d in DEVOPS_DIRS) or ext in (".yml", ".yaml", ".toml", ".ini", ".conf") \
            or "nginx" in base or base.startswith("dockerfile"):
        return ("devops", 0.8)
    if ext in DATA_EXT:
        return ("data", 0.9)
    if ext in DOC_EXT or any(d in low for d in DOC_DIRS):
        return ("docs", 0.8)
    if ext in FRONT_EXT:
        return ("frontend", 1.0)
    if ext in BACK_EXT:
        return ("backend", 1.0)
    if any(d in low for d in FRONT_DIRS):
        return ("frontend", 0.8)
    if any(d in low for d in BACK_DIRS):
        return ("backend", 0.8)
    if ext in (".ts", ".js"):
        return ("backend", 0.4)   # 방향 불명한 TS/JS는 약한 백엔드 표
    return None


def _classify_command(cmd):
    """Bash 명령 → (팀, 확신도)."""
    c = (cmd or "").strip().lower()
    if not c:
        return None
    if re.search(r"\b(pytest|jest|vitest|mocha|playwright|cypress|go test|cargo test|npm test|pnpm test|yarn test|"
                 r"unittest|phpunit|rspec|\bnpx\s+\w*test)\b", c):
        return ("test", 1.0)
    if re.match(r"^\s*(git|gh)\b", c) or re.search(r"\b(docker|kubectl|helm|terraform|systemctl|pm2)\b", c):
        return ("devops", 1.0)
    if re.search(r"\b(npm run build|pnpm build|yarn build|tsc\b|vite build|next build|cargo build|go build|make\b|"
                 r"gradle|mvn|npm install|pnpm install|pip install|deploy|rsync|scp)\b", c):
        return ("devops", 0.9)
    if re.search(r"\b(curl|http|wget|psql|mysql|sqlite3|redis-cli|uvicorn|gunicorn|flask|django|prisma)\b", c):
        return ("backend", 0.7)
    if re.search(r"\b(python3?|node|jupyter)\b.*\.(py|ipynb)\b", c) and re.search(r"pandas|plot|csv|analy", c):
        return ("data", 0.7)
    if re.search(r"\b(grep|rg|find|fd|ls|cat|head|tail|sed -n|wc|tree|mgrep|ugrep)\b", c):
        return ("research", 0.4)
    return ("general", 0.3)


def _classify_tool(name, inp):
    """tool_use 하나 → (팀, 확신도) 또는 None."""
    if not isinstance(inp, dict):
        inp = {}
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return _classify_path(inp.get("file_path")) or ("general", 0.5)
    if name == "Read":
        r = _classify_path(inp.get("file_path"))
        return (r[0], r[1] * 0.5) if r else ("research", 0.3)
    if name in ("Grep", "Glob"):
        r = _classify_path(inp.get("path") or "") if inp.get("path") else None
        return (r[0], 0.4) if r else ("research", 0.5)
    if name == "Bash":
        return _classify_command(inp.get("command"))
    if name in ("Agent", "Task"):
        st = (inp.get("subagent_type") or "").split(":")[-1].lower()
        team = AGENT_TYPE_TEAM.get(st)
        if team:
            return (team, 1.0)
        kw = _classify_keywords(st + " " + (inp.get("description") or ""))
        return kw or ("general", 0.4)
    if name == "Skill":
        sk = (inp.get("skill") or "").split(":")[-1].lower()
        for team, pats in NAME_KEYWORDS:
            if any(re.search(p, sk) for p in pats):
                return (team, 1.0)
        return None
    if name in ("WebSearch", "WebFetch"):
        return ("research", 0.9)
    if name in ("EnterPlanMode", "ExitPlanMode", "AskUserQuestion", "TodoWrite"):
        return ("pm", 0.6)
    if name.startswith("mcp__") and "lsp" in name:
        return ("debug", 0.4)
    if name.startswith("mcp__") and "python_repl" in name:
        return ("data", 0.9)
    return None


def _classify_keywords(text):
    low = (text or "").lower()
    for team, pats in NAME_KEYWORDS:
        if any(re.search(p, low) for p in pats):
            return (team, 1.2)
    return None


def _vote_team(tool_uses, extra=None):
    """최근 tool_use 목록(오래된 → 최신)을 가중 투표해 팀을 정한다."""
    score = {}
    recent = tool_uses[-RECENT_TOOLS:]
    n = len(recent)
    for i, (name, inp) in enumerate(recent):
        r = _classify_tool(name, inp)
        if not r:
            continue
        team, conf = r
        weight = conf * (0.55 + 0.45 * (i + 1) / n)   # 최신일수록 무겁게
        score[team] = score.get(team, 0.0) + weight
    if extra:
        team, conf = extra
        score[team] = score.get(team, 0.0) + conf
    if not score:
        return "general"
    return max(score.items(), key=lambda kv: kv[1])[0]


# ───────────────────────── 트랜스크립트 파싱 ─────────────────────────
def _tool_label(name, inp):
    if not isinstance(inp, dict):
        inp = {}
    try:
        if name in ("Edit", "Write", "Read", "NotebookEdit", "MultiEdit"):
            fp = inp.get("file_path") or ""
            if fp:
                return f"{name}: {os.path.basename(fp)}"
        elif name == "Bash":
            desc = inp.get("description") or (inp.get("command") or "").strip()[:60]
            if desc:
                return f"Bash: {desc}"
        elif name in ("Grep", "Glob"):
            pat = inp.get("pattern") or ""
            if pat:
                return f"{name}: {pat[:40]}"
        elif name in ("Agent", "Task"):
            desc = inp.get("description") or ""
            if desc:
                return f"{name}: {desc[:40]}"
        elif name == "Skill":
            sk = inp.get("skill") or ""
            if sk:
                return f"Skill: {sk}"
        elif name.startswith("mcp__"):
            return name.split("__")[-1]
    except Exception:
        pass
    return name


def _user_text(message):
    """user 라인에서 실제 사용자 프롬프트 텍스트를 뽑는다. 아니면 None."""
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(i, dict) and i.get("type") == "tool_result" for i in content):
            return None
        text = None
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
        if text is None:
            return None
    else:
        return None
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    for bad in ("<", "[Request interrupted", "Caveat:", "Another Claude session sent a message"):
        if stripped.startswith(bad):
            return None
    return stripped


def _read_tail_lines(path, limit=TAIL_BYTES):
    size = path.stat().st_size
    with open(path, "rb") as fh:
        if size > limit:
            fh.seek(size - limit)
            raw = fh.read()
            nl = raw.find(b"\n")          # seek로 잘린 첫 줄은 버린다
            raw = raw[nl + 1:] if nl >= 0 else b""
        else:
            raw = fh.read()
    return raw.decode("utf-8", errors="replace").splitlines()


def _iter_json(lines):
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            yield obj


def _tool_uses_of(obj):
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [(it.get("name") or "?", it.get("input"))
            for it in content if isinstance(it, dict) and it.get("type") == "tool_use"]


def _group_of(cwd, dirname):
    """디렉터리 그룹 = 홈 디렉터리 바로 아래 세그먼트 (예: ~/work/foo → work). 홈 밖이면 첫 세그먼트."""
    seg = None
    if cwd:
        try:
            rel = Path(cwd).expanduser().resolve().relative_to(Path.home().resolve())
            parts = list(rel.parts)
            seg = parts[0] if len(parts) >= 2 else (parts[0] if parts else None)
        except Exception:
            parts = [p for p in re.split(r"[\\/]+", cwd) if p and not p.endswith(":")]
            seg = parts[0] if parts else None
    if not seg:
        toks = [p for p in dirname.split("-") if p]
        seg = toks[2] if len(toks) >= 3 and toks[0] in ("home", "Users") else (toks[0] if toks else None)
    return seg or "기타"


def _project_from_dirname(dirname):
    parts = [p for p in dirname.split("-") if p]
    return "-".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else dirname)


# ───────────────────────── 서브에이전트 ─────────────────────────
def _scan_agents(session_dir, now):
    """<session_dir>/subagents/agent-*.jsonl 중 최근 활동한 것들."""
    sub = session_dir / "subagents"
    if not sub.is_dir():
        return []
    agents = []
    try:
        files = list(sub.glob("agent-*.jsonl"))
    except OSError:
        return []
    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        if now - st.st_mtime > int(CONFIG.get("agentActiveSeconds") or 120):
            continue
        meta = {}
        mf = f.with_name(f.name[:-len(".jsonl")] + ".meta.json")
        try:
            if mf.is_file():
                meta = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        agent_id = f.stem[len("agent-"):]
        action, tool_uses = None, []
        try:
            for obj in _iter_json(_read_tail_lines(f, AGENT_TAIL_BYTES)):
                if obj.get("type") != "assistant":
                    continue
                for name, inp in _tool_uses_of(obj):
                    tool_uses.append((name, inp))
                    action = _tool_label(name, inp)
        except Exception:
            pass
        atype = (meta.get("customAgentType") or meta.get("agentType") or "").split(":")[-1]
        mapped = AGENT_TYPE_TEAM.get(atype.lower())
        if mapped:
            extra = (mapped, 2.0)
        else:
            extra = _classify_keywords(f"{meta.get('agentType', '')} {meta.get('name', '')} {meta.get('description', '')}")
        team = _vote_team(tool_uses, extra)
        agents.append({
            "id": agent_id[-8:],
            "name": meta.get("name") or atype or None,
            "agentType": meta.get("agentType") or atype or None,
            "description": (meta.get("description") or "")[:120] or None,
            "model": meta.get("model"),
            "action": action,
            "team": team,
            "secondsSinceActivity": max(0, int(now - st.st_mtime)),
            "mtime": st.st_mtime,
        })
    agents.sort(key=lambda a: a["mtime"], reverse=True)
    for a in agents:
        del a["mtime"]
    return agents[:MAX_AGENTS_PER_SESSION]


# ───────────────────────── 세션 ─────────────────────────
def _parse_session(path, now):
    st = path.stat()
    info = {
        "id": path.stem,
        "project": _project_from_dirname(path.parent.name),
        "cwd": None,
        "branch": None,
        "secondsSinceActivity": max(0, int(now - st.st_mtime)),
        "status": "idle",
        "lastUserPrompt": None,
        "currentAction": None,
        "currentTool": None,
        "lastAssistantText": None,
        "sizeKB": int(st.st_size / 1024),
        "forkKey": None,
        "recentMessages": [],   # 팀메이트(서브에이전트)와 주고받은 최근 메시지 [{with, dir}]
    }
    age = now - st.st_mtime
    if age <= 180:
        info["status"] = "working"
    elif age <= 1800:
        info["status"] = "recent"

    tool_uses = []
    for obj in _iter_json(_read_tail_lines(path)):
        if obj.get("cwd"):
            info["cwd"] = obj["cwd"]
        if obj.get("gitBranch"):
            info["branch"] = obj["gitBranch"]
        kind = obj.get("type")
        if obj.get("isSidechain"):
            continue
        if kind == "user":
            message = obj.get("message")
            raw = message.get("content") if isinstance(message, dict) else None
            if isinstance(raw, str) and raw.startswith("Another Claude session sent a message"):
                m = re.search(r'teammate_id="([^"]+)"', raw)
                if m:
                    info["recentMessages"].append({"with": m.group(1), "dir": "from"})
                continue
            text = _user_text(message)
            if text:
                info["lastUserPrompt"] = text[:300]
        elif kind == "assistant":
            message = obj.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "tool_use":
                        name, inp = item.get("name") or "?", item.get("input")
                        tool_uses.append((name, inp))
                        if name == "SendMessage" and isinstance(inp, dict) and inp.get("to"):
                            info["recentMessages"].append({"with": str(inp["to"]), "dir": "to"})
                        info["currentAction"] = _tool_label(name, inp)
                        info["currentTool"] = name.split("__")[-1] if name.startswith("mcp__") else name
                    elif item.get("type") == "text":
                        text = (item.get("text") or "").strip()
                        if text:
                            info["lastAssistantText"] = text[:200]

    # 파일 앞부분: 분기(fork) 감지용 첫 레코드 uuid + (꼬리에 없을 때) 첫 사용자 지시
    try:
        with open(path, "rb") as fh:
            head = fh.read(120_000).decode("utf-8", errors="replace").splitlines()[:-1]
        for obj in _iter_json(head):
            if info["forkKey"] is None and obj.get("uuid") and obj.get("type") in ("user", "assistant"):
                info["forkKey"] = obj["uuid"]
            if not info["lastUserPrompt"] and obj.get("type") == "user" and not obj.get("isSidechain"):
                text = _user_text(obj.get("message"))
                if text:
                    info["lastUserPrompt"] = text[:300]
            if info["forkKey"] and info["lastUserPrompt"]:
                break
    except OSError:
        pass

    if info["cwd"]:
        base = os.path.basename(info["cwd"].rstrip("/"))
        if base:
            info["project"] = base
    info["recentMessages"] = info["recentMessages"][-6:]
    info["group"] = _group_of(info["cwd"], path.parent.name)
    info["team"] = _vote_team(tool_uses)
    info["agents"] = _scan_agents(path.parent / path.stem, now)
    return info


def _scan_sessions():
    now = time.time()
    candidates = []
    pdir = projects_dir()
    max_age = float(CONFIG.get("sessionMaxAgeDays") or 7) * 86400
    if pdir.is_dir():
        for proj_dir in pdir.iterdir():
            if not proj_dir.is_dir():
                continue
            for f in proj_dir.glob("*.jsonl"):
                try:
                    st = f.stat()
                except OSError:
                    continue
                if st.st_size < MIN_SIZE_BYTES or now - st.st_mtime > max_age:
                    continue
                candidates.append((st.st_mtime, f))
    candidates.sort(key=lambda pair: pair[0], reverse=True)

    sessions = []
    for _, f in candidates[:int(CONFIG.get("maxSessions") or 12)]:
        try:
            sessions.append(_parse_session(f, now))
        except Exception:
            continue  # 파일 하나가 깨져도 전체 API는 살아 있어야 한다
    # 같은 첫 레코드를 공유하는 세션 = 한 세션에서 분기된 협업 세션
    groups = {}
    for s in sessions:
        if s.get("forkKey"):
            groups.setdefault(s["forkKey"], []).append(s["id"])
    for s in sessions:
        peers = groups.get(s.get("forkKey") or "", [])
        s["forkPeers"] = [pid for pid in peers if pid != s["id"]]
    return {"now": int(now), "teams": TEAMS, "sessions": sessions,
            "projectsDir": str(pdir), "projectsDirExists": pdir.is_dir(),
            "managerName": CONFIG.get("managerName") or "총괄 팀장",
            "pmName": CONFIG.get("pmName") or "PM", "plName": CONFIG.get("plName") or "PL"}


def _payload():
    with _cache_lock:
        if _cache["payload"] is not None and time.time() - _cache["at"] < CACHE_TTL:
            return _cache["payload"]
    data = _scan_sessions()
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    with _cache_lock:
        _cache["at"] = time.time()
        _cache["payload"] = body
    return body


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path == "/" or self.path.startswith("/index.html"):
                body = (BASE_DIR / "index.html").read_bytes()
                self._send(200, "text/html; charset=utf-8", body)
            elif self.path.startswith("/api/sessions"):
                self._send(200, "application/json; charset=utf-8", _payload())
            elif self.path.startswith("/api/config"):
                body = dict(CONFIG)
                body["_configPath"] = str(CONFIG_PATH)
                body["_home"] = str(Path.home())
                body["projectsDirExists"] = projects_dir().is_dir()
                self._send(200, "application/json; charset=utf-8", json.dumps(body, ensure_ascii=False).encode("utf-8"))
            else:
                self._send(404, "text/plain; charset=utf-8", "not found".encode())
        except BrokenPipeError:
            pass
        except Exception as exc:
            try:
                self._send(500, "text/plain; charset=utf-8", str(exc).encode())
            except Exception:
                pass

    def do_POST(self):
        try:
            if not self.path.startswith("/api/config"):
                self._send(404, "text/plain; charset=utf-8", b"not found")
                return
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if not isinstance(data, dict):
                raise ValueError("object expected")
            with _config_lock:
                for k in DEFAULT_CONFIG:
                    if k in data:
                        v = data[k]
                        if isinstance(DEFAULT_CONFIG[k], int) and not isinstance(DEFAULT_CONFIG[k], bool):
                            v = int(v)
                        elif isinstance(DEFAULT_CONFIG[k], str):
                            v = str(v).strip() or DEFAULT_CONFIG[k]
                        CONFIG[k] = v
                save_config(CONFIG)
                _cache["payload"] = None
            self._send(200, "application/json; charset=utf-8",
                       json.dumps({"ok": True, "projectsDirExists": projects_dir().is_dir(),
                                   "portChangeNeedsRestart": int(CONFIG.get("port") or 8765) != PORT},
                                  ensure_ascii=False).encode("utf-8"))
        except Exception as exc:
            self._send(400, "application/json; charset=utf-8",
                       json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"))

    def log_message(self, *args):
        pass  # 조용히


def main():
    global PORT
    for i, arg in enumerate(sys.argv):
        if arg in ("-p", "--port") and i + 1 < len(sys.argv):
            PORT = int(sys.argv[i + 1])
    pdir = projects_dir()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("🏢 Claude Office 서버 시작!")
    print(f"   브라우저에서 열기 → http://localhost:{PORT}")
    print(f"   세션 디렉터리  → {pdir}" + ("" if pdir.is_dir() else "  ⚠ 없음! 우상단 ⚙ 설정에서 경로를 지정하세요"))
    print(f"   설정 파일      → {CONFIG_PATH}")
    print("   종료: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n퇴근합니다. 👋")


if __name__ == "__main__":
    main()
