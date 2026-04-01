#!/usr/bin/env python3
"""
Работа с .env для systemd EnvironmentFile и для загрузки в bash без `source`
(значения вроде SECRET_KEY с ) & ! # $ не ломают shell).
"""
from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path


def systemd_double_quote(value: str) -> str:
    inner = value.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")
    return f'"{inner}"'


def parse_value(raw: str) -> str:
    """Значение справа от первого = (без ключа)."""
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] == '"':
        out: list[str] = []
        i = 1
        while i < len(raw):
            if raw[i] == "\\" and i + 1 < len(raw):
                n = raw[i + 1]
                if n == "n":
                    out.append("\n")
                elif n in ('"', "\\"):
                    out.append(n)
                else:
                    out.append(raw[i])
                    out.append(n)
                i += 2
                continue
            if raw[i] == '"':
                break
            out.append(raw[i])
            i += 1
        return "".join(out)
    if raw[0] == "'":
        raw = raw.strip()
        if len(raw) >= 2 and raw[-1] == "'":
            return raw[1:-1].replace("''", "'")
        return raw[1:]
    return raw


def parse_env_lines(text: str) -> list[tuple[str, str]]:
    """Порядок как в файле; комментарии/пустые строки пропускаются."""
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        k, _, rest = s.partition("=")
        k = k.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
            continue
        rows.append((k, parse_value(rest)))
    return rows


def cmd_export(path: Path) -> None:
    if not path.is_file():
        sys.exit(f"envtool export: файл не найден: {path}")
    text = path.read_text(encoding="utf-8")
    seen: set[str] = set()
    # последнее значение ключа побеждает, как в типичном .env
    pairs: list[tuple[str, str]] = []
    for k, v in parse_env_lines(text):
        if k in seen:
            pairs = [(a, b) for a, b in pairs if a != k]
            seen.discard(k)
        pairs.append((k, v))
        seen.add(k)
    for k, v in pairs:
        print(f"export {k}={shlex.quote(v)}")


def cmd_materialize(path: Path) -> None:
    if not path.is_file():
        return
    prev_stat = path.stat()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    out_lines: list[str] = []
    for line in raw_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out_lines.append(line)
            continue
        if "=" not in stripped:
            out_lines.append(line)
            continue
        k, _, rest = stripped.partition("=")
        k = k.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
            out_lines.append(line)
            continue
        v = parse_value(rest)
        out_lines.append(f"{k}={systemd_double_quote(v)}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
    tmp.replace(path)
    # После replace владелец мог стать root (если вызвали sudo без -u appuser) — вернём как было.
    try:
        os.chown(path, prev_stat.st_uid, prev_stat.st_gid)
    except OSError:
        pass


def cmd_append(path: Path, key: str, value: str) -> None:
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
        sys.exit("envtool append: неверное имя ключа")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{key}={systemd_double_quote(value)}\n")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: envtool.py export|materialize|append PATH [KEY VALUE]")
    cmd = sys.argv[1]
    if cmd == "export":
        if len(sys.argv) != 3:
            sys.exit("usage: envtool.py export PATH")
        cmd_export(Path(sys.argv[2]))
        return
    if cmd == "materialize":
        if len(sys.argv) != 3:
            sys.exit("usage: envtool.py materialize PATH")
        p = Path(sys.argv[2])
        if not p.is_file():
            return
        cmd_materialize(p)
        return
    if cmd == "append":
        if len(sys.argv) != 5:
            sys.exit("usage: envtool.py append PATH KEY VALUE")
        cmd_append(Path(sys.argv[2]), sys.argv[3], sys.argv[4])
        return
    sys.exit(f"unknown command: {cmd}")


if __name__ == "__main__":
    main()
