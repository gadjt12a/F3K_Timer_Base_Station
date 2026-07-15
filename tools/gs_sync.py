#!/usr/bin/env python3
"""
gs_sync.py — GliderScore direct .mdb write bridge (Windows only).

Reads competition flight data from the F3K base station HTTP API and writes
it directly to GliderScore's Access database via a 32-bit PowerShell subprocess
(ACE OLEDB provider is 32-bit only on this machine).

GliderScore must have the competition drawn up first (pilots assigned to groups).
This script UPDATEs existing Scores rows — it does NOT insert new ones.

CLI usage:
    python tools/gs_sync.py --comp-id 1
    python tools/gs_sync.py --comp-id 1 --round 3
    python tools/gs_sync.py --comp-id 1 --dry-run
    python tools/gs_sync.py --comp-id 1 --base http://f3kbase.local:8080

GUI usage (double-click the .exe or run with no arguments):
    python tools/gs_sync.py

Defaults: base=http://10.0.1.12:8080, mdb=C:\\GliderScore6\\GliderScoreData.mdb
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

MDB_DEFAULT = r"C:\GliderScore6\GliderScoreData.mdb"
PS32 = r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"
TASK_NO = {"F3K": 5, "F5K": 6}


# ---------------------------------------------------------------------------
# F3K task scoring
# ---------------------------------------------------------------------------

def _parse_task(task_str: str) -> tuple[str, str]:
    if "(" in task_str:
        letter, rest = task_str.split("(", 1)
        variant = rest.rstrip(")")
    else:
        letter, variant = task_str, ""
    return letter.upper(), variant


def f3k_scored(task_str: str, flights_ms: list[int]) -> list[float]:
    """
    Apply F3K task rules. Returns scored flight times in SECONDS (float),
    in storage order (Laps → T1M → T1S → T2M → T2S).
    Flights_ms are in chronological order (flight_no ascending).
    """
    letter, variant = _parse_task(task_str)
    flights_s = [f / 1000.0 for f in flights_ms]

    def cap(s: float, max_s: float) -> float:
        return min(s, max_s)

    if letter == "A":
        last = flights_s[-1] if flights_s else 0.0
        return [cap(last, 300.0)]

    if letter == "B":
        max_s = 240.0 if variant == "1" else 180.0
        last2 = flights_s[-2:] if len(flights_s) >= 2 else flights_s
        return [cap(f, max_s) for f in last2]

    if letter == "C":
        n = {"1": 3, "2": 4, "3": 5}.get(variant, 3)
        return [cap(f, 180.0) for f in flights_s[:n]]

    if letter == "F":
        best3 = sorted(flights_s, reverse=True)[:3]
        return [cap(f, 180.0) for f in best3]

    if letter == "G":
        best5 = sorted(flights_s, reverse=True)[:5]
        return [cap(f, 120.0) for f in best5]

    if letter == "H":
        return _match_targets(flights_s, [60.0, 120.0, 180.0, 240.0])

    if letter == "I":
        best3 = sorted(flights_s, reverse=True)[:3]
        return [cap(f, 200.0) for f in best3]

    if letter == "J":
        last3 = flights_s[-3:] if len(flights_s) >= 3 else flights_s
        return [cap(f, 180.0) for f in last3]

    if letter == "L":
        best = max(flights_s) if flights_s else 0.0
        return [cap(best, 599.0)]

    if letter == "N":
        best = max(flights_s) if flights_s else 0.0
        return [cap(best, 300.0)]

    return sorted(flights_s, reverse=True)[:5]


def _match_targets(flights_s: list[float], targets_s: list[float]) -> list[float]:
    desc_flights = sorted(flights_s, reverse=True)
    desc_targets = sorted(targets_s, reverse=True)
    pairs = []
    for i, t in enumerate(desc_targets):
        f = desc_flights[i] if i < len(desc_flights) else 0.0
        pairs.append((t, min(f, t)))
    pairs.sort()
    return [s for _, s in pairs]


def _to_mmss(s: float) -> float:
    """Seconds → mmss.t format.  288.2 s → 448.2  (4 min 48.2 sec)."""
    mins = int(s) // 60
    secs = s - mins * 60
    return round(mins * 100 + secs, 1)


# ---------------------------------------------------------------------------
# F5K scoring
# ---------------------------------------------------------------------------

def f5k_height_bonus(alt_m: float, ref_height: float) -> float:
    diff = alt_m - ref_height
    if diff <= 0:
        return round(abs(diff) * 0.5, 1)
    if diff <= 10:
        return round(-diff * 1.0, 1)
    return round(-(10.0 + (diff - 10) * 3.0), 1)


def _fmt_msc(s: float) -> str:
    total_s = int(s)
    mins, secs = divmod(total_s, 60)
    return f"{mins}:{secs:02d}" if mins > 0 else str(total_s)


def f5k_scored(task_str: str, flights: list[dict], ref_height: float) -> list[dict]:
    letter, _ = _parse_task(task_str)
    flights_s = [f["duration_ms"] / 1000.0 for f in flights]
    alts = [f.get("altitude_m") or 0.0 for f in flights]

    def slot(fno: int, s: float, tpt: float, alt: float) -> dict:
        hpt = f5k_height_bonus(alt, ref_height) if alt > 0 else 0.0
        return {
            "fno": fno,
            "ftm_mmss": _to_mmss(s),
            "ftm_msc": _fmt_msc(s),
            "tpt_s": round(tpt, 1),
            "hva_m": int(alt),
            "hpt": hpt,
            "fpt": round(tpt + hpt, 1),
        }

    def empty_slot(fno: int) -> dict:
        return {"fno": fno, "ftm_mmss": 0, "ftm_msc": "0",
                "tpt_s": 0, "hva_m": 0, "hpt": 0, "fpt": 0}

    if letter == "B":
        if flights_s:
            s = flights_s[-1]
            return [slot(1, s, min(s, 300.0), alts[-1] if alts else 0.0)]
        return [empty_slot(1)]

    if letter == "C":
        out = []
        for i in range(3):
            if i < len(flights_s):
                s, alt = flights_s[i], alts[i]
                out.append(slot(i + 1, s, min(s, 240.0), alt))
            else:
                out.append(empty_slot(i + 1))
        return out

    if letter == "A":
        targets = [60.0, 120.0, 180.0, 240.0]
        return _f5k_match(flights_s, alts, targets, ref_height)

    if letter == "D":
        targets = [180.0, 180.0, 240.0]
        return _f5k_match(flights_s, alts, targets, ref_height)

    out = []
    for i, (s, alt) in enumerate(zip(flights_s, alts), 1):
        out.append(slot(i, s, s, alt))
    return out


def _f5k_match(flights_s: list[float], alts: list[float],
               targets: list[float], ref_height: float) -> list[dict]:
    n_targets = len(targets)
    n_flights = len(flights_s)

    ranked_flights = sorted(range(n_flights), key=lambda i: flights_s[i], reverse=True)
    scored_indices = set(ranked_flights[:n_targets])
    sorted_targets_desc = sorted(targets, reverse=True)

    assignment: dict[int, float] = {}
    for rank, f_idx in enumerate(ranked_flights[:n_targets]):
        t = sorted_targets_desc[rank]
        assignment[f_idx] = min(flights_s[f_idx], t)

    result = []
    fno = 1
    for chron_idx in range(n_flights):
        if chron_idx not in scored_indices:
            continue
        s = flights_s[chron_idx]
        alt = alts[chron_idx] if chron_idx < len(alts) else 0.0
        tpt = assignment[chron_idx]
        hpt = f5k_height_bonus(alt, ref_height) if alt > 0 else 0.0
        result.append({
            "fno": fno,
            "ftm_mmss": _to_mmss(s),
            "ftm_msc": _fmt_msc(s),
            "tpt_s": round(tpt, 1),
            "hva_m": int(alt),
            "hpt": hpt,
            "fpt": round(tpt + hpt, 1),
        })
        fno += 1

    for i in range(len(result), n_targets):
        result.append({"fno": i + 1, "ftm_mmss": 0, "ftm_msc": "0",
                        "tpt_s": 0, "hva_m": 0, "hpt": 0, "fpt": 0})

    return result


def _f5k_flight_string(slot: dict, is_first: bool, is_last: bool) -> str:
    nof = "" if is_last else "1"
    updated = "&Updated=True" if is_first else ""
    return (
        f"FNO={slot['fno']}&MID=&FTM={slot['ftm_mmss']}&MSC={slot['ftm_msc']}&"
        f"TPT={slot['tpt_s']}&HVA={slot['hva_m']}&HPT={slot['hpt']}&"
        f"NOF={nof}&NFP=0&LOT=False&LOP=0&LLN=False&LLP=0&OOF=False&"
        f"MOS=False&HPN=False&SFY=False&SFP=0&FPT={slot['fpt']}{updated}&"
    )


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------

def build_f3k_row(comp_no: int, round_no: int, group_no: int,
                  pilot_no: int, task_str: str, flights_ms: list[int]) -> dict:
    scored = f3k_scored(task_str, flights_ms)
    mmss = [_to_mmss(s) for s in scored[:5]]
    while len(mmss) < 5:
        mmss.append(0.0)
    raw_s = sum(scored)
    return {
        "comp_no": comp_no, "task_no": TASK_NO["F3K"],
        "round_no": round_no, "group_no": group_no, "pilot_no": pilot_no,
        "laps": mmss[0], "t1m": mmss[1], "t1s": mmss[2], "t2m": mmss[3], "t2s": mmss[4],
        "raw_score": round(raw_s, 1),
        "flight1": "", "flight2": "", "flight3": "", "flight4": "",
    }


def build_f5k_row(comp_no: int, round_no: int, group_no: int,
                  pilot_no: int, task_str: str,
                  flights: list[dict], ref_height: float) -> dict:
    slots = f5k_scored(task_str, flights, ref_height)
    fpts = [s["fpt"] for s in slots]
    while len(fpts) < 4:
        fpts.append(0.0)

    flight_strings = [
        _f5k_flight_string(s, is_first=(i == 0), is_last=(i == len(slots) - 1))
        for i, s in enumerate(slots)
    ]
    while len(flight_strings) < 4:
        flight_strings.append("")

    return {
        "comp_no": comp_no, "task_no": TASK_NO["F5K"],
        "round_no": round_no, "group_no": group_no, "pilot_no": pilot_no,
        "laps": fpts[0], "t1m": fpts[1], "t1s": fpts[2], "t2m": fpts[3], "t2s": 0.0,
        "raw_score": round(sum(s["fpt"] for s in slots), 1),
        "flight1": flight_strings[0], "flight2": flight_strings[1],
        "flight3": flight_strings[2], "flight4": flight_strings[3],
    }


# ---------------------------------------------------------------------------
# PowerShell DB operations
# ---------------------------------------------------------------------------

def _ps_str(s: str) -> str:
    return s.replace("'", "''")


def _read_ref_heights_ps(mdb_path: str, comp_no: int) -> str:
    return f"""
$conn = New-Object System.Data.OleDb.OleDbConnection('Provider=Microsoft.ACE.OLEDB.12.0;Data Source={_ps_str(mdb_path)};')
$conn.Open()
$cmd = $conn.CreateCommand()
$cmd.CommandText = 'SELECT RoundNo, RefHeight FROM F5KTaskandRefHeightByRound WHERE CompNo={comp_no}'
$r = $cmd.ExecuteReader()
$rows = @()
while ($r.Read()) {{ $rows += '{{"round_no":' + $r['RoundNo'] + ',"ref_height":' + $r['RefHeight'] + '}}' }}
$r.Close()
$conn.Close()
Write-Output ('[' + ($rows -join ',') + ']')
"""


def _write_scores_ps(mdb_path: str, rows: list[dict]) -> str:
    lines = [
        f"$conn = New-Object System.Data.OleDb.OleDbConnection('Provider=Microsoft.ACE.OLEDB.12.0;Data Source={_ps_str(mdb_path)};Mode=ReadWrite;')",
        "$conn.Open()",
        "$cmd = $conn.CreateCommand()",
        "$ok = 0; $miss = 0",
        "",
    ]
    for row in rows:
        cn, tn = row["comp_no"], row["task_no"]
        rn, gn, pn = row["round_no"], row["group_no"], row["pilot_no"]
        laps, t1m, t1s, t2m, t2s = row["laps"], row["t1m"], row["t1s"], row["t2m"], row["t2s"]
        raw = row["raw_score"]
        f1 = _ps_str(row["flight1"])
        f2 = _ps_str(row["flight2"])
        f3 = _ps_str(row["flight3"])
        f4 = _ps_str(row["flight4"])
        lines += [
            f"$cmd.CommandText = 'SELECT COUNT(*) FROM Scores WHERE CompNo={cn} AND TaskNo={tn} AND RoundNo={rn} AND GroupNo={gn} AND ReFlightNo=0 AND PilotNo={pn}'",
            "$n = $cmd.ExecuteScalar()",
            "if ($n -gt 0) {",
            f"    $cmd.CommandText = 'UPDATE Scores SET Laps={laps}, Time1Mins={t1m}, Time1Secs={t1s}, Time2Mins={t2m}, Time2Secs={t2s}, RawScore={raw}, NormalisedScore=0, Flight1=''{f1}'', Flight2=''{f2}'', Flight3=''{f3}'', Flight4=''{f4}'' WHERE CompNo={cn} AND TaskNo={tn} AND RoundNo={rn} AND GroupNo={gn} AND ReFlightNo=0 AND PilotNo={pn}'",
            "    $cmd.ExecuteNonQuery() | Out-Null",
            "    $ok++",
            "} else {",
            f"    Write-Host 'MISS: P{pn} R{rn}G{gn} not in draw'",
            "    $miss++",
            "}",
            "",
        ]
    lines += [
        "$conn.Close()",
        'Write-Host "Updated: $ok  Missing from draw: $miss"',
    ]
    return "\n".join(lines)


def _run_ps32(script: str) -> tuple[str, str, int]:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".ps1", delete=False,
                                     encoding="utf-8") as f:
        f.write(script)
        path = f.name
    try:
        r = subprocess.run(
            [PS32, "-NonInteractive", "-File", path],
            capture_output=True, text=True, timeout=60,
        )
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Core sync logic (called by both CLI and GUI)
# ---------------------------------------------------------------------------

def _run_sync(comp_id: int, round_no: int, mdb: str, base: str, dry_run: bool) -> None:
    """
    Fetch flight data from the base station and write to GliderScore .mdb.
    Raises RuntimeError on fatal errors. Uses print() for all output.
    """
    url = f"{base.rstrip('/')}/export/{comp_id}/json"
    print(f"Fetching {url} ...")
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot reach base station: {exc}") from exc

    comp_no = data.get("gliderscore_comp_no")
    if not comp_no:
        raise RuntimeError(
            "Competition has no GliderScore CompNo — import from GliderScore first."
        )

    ref_heights: dict[int, float] = {}
    if any(r["discipline"] == "F5K" for r in data["rounds"]):
        print("Reading F5K RefHeights from .mdb ...")
        script = _read_ref_heights_ps(mdb, comp_no)
        out, err, rc = _run_ps32(script)
        if rc != 0:
            print(f"  WARN: Could not read RefHeights ({err}); defaulting to 60m")
        else:
            try:
                for row in json.loads(out):
                    ref_heights[row["round_no"]] = float(row["ref_height"])
            except Exception:
                print(f"  WARN: Could not parse RefHeights ({out!r}); defaulting to 60m")

    rows = []
    for rnd in data["rounds"]:
        if round_no and rnd["round_no"] != round_no:
            continue
        discipline = rnd["discipline"]
        task_str = rnd["task"]
        ref_h = ref_heights.get(rnd["round_no"], 60.0)

        for grp in rnd["groups"]:
            for pilot in grp["pilots"]:
                pno = pilot["gliderscore_pilot_no"]
                if not pno:
                    print(f"  SKIP: '{pilot['name']}' has no GS pilot no")
                    continue
                flights = pilot["flights"]
                if not flights:
                    continue

                if discipline == "F3K":
                    row = build_f3k_row(
                        comp_no, rnd["round_no"], grp["group_no"],
                        pno, task_str, [f["duration_ms"] for f in flights],
                    )
                else:
                    row = build_f5k_row(
                        comp_no, rnd["round_no"], grp["group_no"],
                        pno, task_str, flights, ref_h,
                    )

                rows.append(row)
                tag = f"R{rnd['round_no']}G{grp['group_no']} {discipline} P{pno} ({pilot['name']})"
                print(f"  {tag}: Raw={row['raw_score']}  Laps={row['laps']}  T1M={row['t1m']}")

    if not rows:
        print("No rows to write.")
        return

    if dry_run:
        print(f"\nDry run — {len(rows)} rows ready.  Re-run without Dry Run to write.")
        return

    print(f"\nWriting {len(rows)} rows to {mdb} ...")
    script = _write_scores_ps(mdb, rows)
    out, err, rc = _run_ps32(script)
    if out:
        print(out)
    if err:
        print(f"STDERR: {err}", file=sys.stderr)
    if rc != 0:
        raise RuntimeError(f"PowerShell exited {rc}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Sync F3K base station scores → GliderScore .mdb")
    ap.add_argument("--base", default="http://10.0.1.12:8080")
    ap.add_argument("--comp-id", type=int, required=True)
    ap.add_argument("--mdb", default=MDB_DEFAULT)
    ap.add_argument("--round", type=int, default=0, help="Sync one round only (0 = all)")
    ap.add_argument("--dry-run", action="store_true", help="Print rows, don't write")
    args = ap.parse_args()

    try:
        _run_sync(args.comp_id, args.round, args.mdb, args.base, args.dry_run)
    except RuntimeError as exc:
        sys.exit(f"ERROR: {exc}")


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class _LogRedirect:
    """Thread-safe stdout redirect: writes lines into a queue."""

    def __init__(self, q: "queue.Queue[str]") -> None:
        self._q = q
        self._buf = ""

    def write(self, s: str) -> None:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._q.put(line)

    def flush(self) -> None:
        if self._buf:
            self._q.put(self._buf)
            self._buf = ""


class SyncApp:
    _CONFIG_PATH = Path(os.environ.get("APPDATA", str(Path.home()))) / "F3KSync" / "config.json"

    def __init__(self, root) -> None:  # root: tkinter.Tk (imported lazily)
        import tkinter as tk
        from tkinter import ttk
        self._tk = tk
        self._ttk = ttk

        self.root = root
        root.title("F3K → GliderScore Sync")
        root.resizable(True, True)
        root.minsize(540, 500)

        self._cfg = self._load_config()
        self._comp_list: list[dict] = []
        self._round_nos: list[int] = [0]
        self._running = False
        self._log_q: queue.Queue[str] = queue.Queue()

        self._build_ui()
        self._apply_config()
        self._poll_log()

    def _build_ui(self) -> None:
        tk = self._tk
        ttk = self._ttk

        # GliderScore folder
        frm_gs = ttk.LabelFrame(self.root, text="GliderScore", padding=8)
        frm_gs.pack(fill="x", padx=10, pady=(10, 4))

        ttk.Label(frm_gs, text="Folder:").grid(row=0, column=0, sticky="w")
        self._mdb_var = tk.StringVar()
        ttk.Entry(frm_gs, textvariable=self._mdb_var, width=44).grid(
            row=0, column=1, sticky="ew", padx=(6, 4))
        ttk.Button(frm_gs, text="Browse…", command=self._browse_folder).grid(row=0, column=2)
        frm_gs.columnconfigure(1, weight=1)

        # Base station
        frm_bs = ttk.LabelFrame(self.root, text="Base Station", padding=8)
        frm_bs.pack(fill="x", padx=10, pady=4)

        ttk.Label(frm_bs, text="URL:").grid(row=0, column=0, sticky="w")
        self._url_var = tk.StringVar()
        ttk.Entry(frm_bs, textvariable=self._url_var, width=38).grid(
            row=0, column=1, sticky="ew", padx=(6, 4))
        ttk.Button(frm_bs, text="Connect", command=self._connect).grid(row=0, column=2)
        frm_bs.columnconfigure(1, weight=1)

        # Competition + round
        frm_comp = ttk.LabelFrame(self.root, text="Competition", padding=8)
        frm_comp.pack(fill="x", padx=10, pady=4)

        ttk.Label(frm_comp, text="Competition:").grid(row=0, column=0, sticky="w")
        self._comp_var = tk.StringVar()
        self._comp_cb = ttk.Combobox(frm_comp, textvariable=self._comp_var,
                                     state="readonly", width=42)
        self._comp_cb.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self._comp_cb.bind("<<ComboboxSelected>>", self._on_comp_selected)

        ttk.Label(frm_comp, text="Round:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._round_var = tk.StringVar()
        self._round_cb = ttk.Combobox(frm_comp, textvariable=self._round_var,
                                      state="readonly", width=42)
        self._round_cb.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=(6, 0))
        frm_comp.columnconfigure(1, weight=1)

        # Action buttons
        frm_btn = ttk.Frame(self.root, padding=(10, 6))
        frm_btn.pack(fill="x")
        self._btn_sync = ttk.Button(frm_btn, text="Sync Scores",
                                    command=self._sync, state="disabled")
        self._btn_sync.pack(side="left", padx=(0, 6))
        self._btn_dry = ttk.Button(frm_btn, text="Dry Run",
                                   command=self._dry_run, state="disabled")
        self._btn_dry.pack(side="left")

        # Log output
        from tkinter.scrolledtext import ScrolledText
        frm_log = ttk.LabelFrame(self.root, text="Output", padding=8)
        frm_log.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self._log_widget = ScrolledText(
            frm_log, height=12, state="disabled",
            font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="white",
        )
        self._log_widget.pack(fill="both", expand=True)

    def _apply_config(self) -> None:
        self._mdb_var.set(self._cfg.get("mdb_path", MDB_DEFAULT))
        self._url_var.set(self._cfg.get("base_url", "http://10.0.1.12:8080"))

    def _load_config(self) -> dict:
        try:
            return json.loads(self._CONFIG_PATH.read_text())
        except Exception:
            return {}

    def _save_config(self) -> None:
        self._CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        cfg = {
            "mdb_path": self._mdb_var.get(),
            "base_url": self._url_var.get(),
            "last_comp_id": self._cfg.get("last_comp_id"),
        }
        self._CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

    def _browse_folder(self) -> None:
        from tkinter import filedialog
        current = self._mdb_var.get()
        initial = str(Path(current).parent) if current else r"C:\\"
        folder = filedialog.askdirectory(
            title="Select GliderScore installation folder",
            initialdir=initial,
        )
        if folder:
            self._mdb_var.set(str(Path(folder) / "GliderScoreData.mdb"))

    def _log(self, msg: str) -> None:
        self._log_widget.configure(state="normal")
        self._log_widget.insert("end", msg + "\n")
        self._log_widget.see("end")
        self._log_widget.configure(state="disabled")

    def _poll_log(self) -> None:
        try:
            while True:
                self._log(self._log_q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(50, self._poll_log)

    def _connect(self) -> None:
        base = self._url_var.get().rstrip("/")
        self._log(f"Connecting to {base} …")
        try:
            with urllib.request.urlopen(f"{base}/api/competitions", timeout=5) as r:
                self._comp_list = json.loads(r.read())
        except Exception as exc:
            self._log(f"ERROR: {exc}")
            return

        if not self._comp_list:
            self._log("No competitions found on base station.")
            return

        labels = [
            f"{c['id']} — {c['name']} ({c['discipline']})"
            for c in self._comp_list
        ]
        self._comp_cb["values"] = labels
        self._comp_cb.current(0)
        self._on_comp_selected()
        self._save_config()
        self._log(f"Found {len(self._comp_list)} competition(s). Select one and click Sync.")

    def _on_comp_selected(self, _event=None) -> None:
        idx = self._comp_cb.current()
        if idx < 0 or idx >= len(self._comp_list):
            return
        comp = self._comp_list[idx]
        self._cfg["last_comp_id"] = comp["id"]

        base = self._url_var.get().rstrip("/")
        try:
            with urllib.request.urlopen(f"{base}/export/{comp['id']}/json", timeout=5) as r:
                data = json.loads(r.read())
            rounds = data.get("rounds", [])
            round_labels = ["All rounds"] + [
                f"Round {rnd['round_no']} — {rnd['task']} ({rnd['discipline']})"
                for rnd in rounds
            ]
            self._round_nos = [0] + [rnd["round_no"] for rnd in rounds]
        except Exception:
            round_labels = ["All rounds"]
            self._round_nos = [0]

        self._round_cb["values"] = round_labels
        self._round_cb.current(0)
        self._btn_sync["state"] = "normal"
        self._btn_dry["state"] = "normal"

    def _selected_comp_id(self) -> int:
        return self._comp_list[self._comp_cb.current()]["id"]

    def _selected_round(self) -> int:
        idx = self._round_cb.current()
        return self._round_nos[idx] if idx >= 0 else 0

    def _sync(self) -> None:
        self._run_sync(dry_run=False)

    def _dry_run(self) -> None:
        self._run_sync(dry_run=True)

    def _run_sync(self, dry_run: bool) -> None:
        if self._running:
            return
        self._running = True
        self._btn_sync["state"] = "disabled"
        self._btn_dry["state"] = "disabled"

        self._log_widget.configure(state="normal")
        self._log_widget.delete("1.0", "end")
        self._log_widget.configure(state="disabled")

        t = threading.Thread(
            target=self._sync_thread,
            args=(
                self._selected_comp_id(),
                self._selected_round(),
                self._mdb_var.get(),
                self._url_var.get(),
                dry_run,
            ),
            daemon=True,
        )
        t.start()

    def _sync_thread(self, comp_id: int, round_no: int,
                     mdb: str, base: str, dry_run: bool) -> None:
        orig_stdout = sys.stdout
        sys.stdout = _LogRedirect(self._log_q)
        try:
            _run_sync(comp_id, round_no, mdb, base, dry_run)
        except RuntimeError as exc:
            self._log_q.put(f"ERROR: {exc}")
        except Exception as exc:
            self._log_q.put(f"UNEXPECTED ERROR: {exc}")
        finally:
            sys.stdout = orig_stdout
            self._running = False
            self.root.after(0, self._on_sync_done)

    def _on_sync_done(self) -> None:
        if self._comp_cb.current() >= 0:
            self._btn_sync["state"] = "normal"
            self._btn_dry["state"] = "normal"


def launch_gui() -> None:
    import tkinter as tk
    root = tk.Tk()
    SyncApp(root)
    root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if any(a in sys.argv for a in ("--comp-id", "--help", "-h")):
        main()
    else:
        launch_gui()
