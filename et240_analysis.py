"""Shared CY4500 offline PPS/AVS analysis (MIT; see LICENSE).
Copied from the local CY4500 CLI; this module performs no USB access.
"""
import csv
import json
import math
import statistics
from dataclasses import asdict
from pathlib import Path
from typing import Optional
from ezpd_protocol import *
from pd_capture import load_pd_capture_csv
TRANSITION_CSV_COLUMNS = (
    "request_row_index",
    "request_sno",
    "direction",
    "target_voltage_V",
    "requested_current_A",
    "request_start_us",
    "request_end_us",
    "accept_start_us",
    "accept_latency_us",
    "ps_rdy_start_us",
    "ps_rdy_latency_us",
    "request_scope_timestamp_us",
    "request_scope_vbus_V",
    "ps_rdy_scope_timestamp_us",
    "ps_rdy_scope_vbus_V",
    "baseline_vbus_V",
    "baseline_noise_mad_V",
    "movement_threshold_V",
    "movement_start_us",
    "movement_latency_us",
    "target_crossing_us",
    "target_crossing_latency_us",
    "target_band_first_entry_us",
    "target_band_first_entry_latency_us",
    "settling_us",
    "settling_latency_us",
    "settling_hold_us",
    "target_band_fraction",
    "average_slew_V_per_s",
    "observed_plateau_V",
    "observed_plateau_mad_V",
    "observed_plateau_sample_count",
    "observed_band_fraction",
    "observed_band_first_entry_us",
    "observed_band_first_entry_latency_us",
    "observed_settling_us",
    "observed_settling_latency_us",
    "observed_average_slew_V_per_s",
    "flags",
    "request_mode",
    "request_message",
    "object_position",
)

def _optional_int(value):
    if value is None or value == "":
        return None
    try:
        if math.isnan(float(value)):
            return None
    except Exception:
        pass
    return int(float(value))

def _optional_float(value):
    if value is None or value == "":
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) else value

def _load_pd_capture_csv(path: Path) -> list[SyncPDSample]:
    return load_pd_capture_csv(path)

def _load_scope_csv(path: Path) -> list[SyncScopeSample]:
    samples: list[SyncScopeSample] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            timestamp_us = _optional_int(row.get("Timestamp(us)"))
            vbus_v = _optional_float(row.get("Vbus(V)"))
            if timestamp_us is None or vbus_v is None:
                continue
            samples.append(
                SyncScopeSample(
                    timestamp_us=timestamp_us,
                    vbus_V=vbus_v,
                    ibus_A=_optional_float(row.get("Ibus(A)")),
                    cc1_V=_optional_float(row.get("CC1(V)")),
                    cc2_V=_optional_float(row.get("CC2(V)")),
                )
            )
    samples.sort(key=lambda s: s.timestamp_us)
    return samples

def _transition_row(a: AVSTransitionAnalysis) -> dict[str, object]:
    row = {}
    for name in TRANSITION_CSV_COLUMNS:
        if name == "flags":
            row[name] = ";".join(a.flags)
        else:
            row[name] = getattr(a, name)
    return row

HUMAN_TRANSITION_SUMMARY_COLUMNS = (
    "transition",
    "request_mode",
    "request_message",
    "object_position",
    "request_sno",
    "direction",
    "from_V",
    "target_V",
    "observed_plateau_V",
    "target_minus_plateau_mV",
    "accept_ms",
    "movement_start_ms",
    "ps_rdy_ms",
    "vbus_at_ps_rdy_V",
    "ps_rdy_vs_plateau_percent",
    "ps_rdy_error_from_plateau_mV",
    "absolute_settle_status",
    "absolute_settle_ms",
    "ps_rdy_to_absolute_settle_ms",
    "absolute_slew_V_per_s",
    "observed_settle_status",
    "observed_settle_ms",
    "ps_rdy_to_observed_settle_ms",
    "movement_to_observed_settle_ms",
    "observed_slew_V_per_s",
    "flags",
)

def _ms_from_us(value):
    return None if value is None else float(value) / 1000.0

def _human_summary_row(
    transition_no: int,
    a: AVSTransitionAnalysis,
) -> dict[str, object]:
    plateau = a.observed_plateau_V
    ps_v = a.ps_rdy_scope_vbus_V

    target_minus_plateau_mv = (
        None
        if plateau is None
        else (a.target_voltage_V - plateau) * 1000.0
    )

    ps_rdy_vs_plateau_percent = (
        None
        if plateau in (None, 0.0) or ps_v is None
        else ps_v / plateau * 100.0
    )

    ps_rdy_error_from_plateau_mv = (
        None
        if plateau is None or ps_v is None
        else (ps_v - plateau) * 1000.0
    )

    ps_to_abs_ms = (
        None
        if a.ps_rdy_start_us is None or a.settling_us is None
        else (a.settling_us - a.ps_rdy_start_us) / 1000.0
    )
    ps_to_obs_ms = (
        None
        if a.ps_rdy_start_us is None or a.observed_settling_us is None
        else (a.observed_settling_us - a.ps_rdy_start_us) / 1000.0
    )
    move_to_obs_ms = (
        None
        if a.movement_start_us is None or a.observed_settling_us is None
        else (a.observed_settling_us - a.movement_start_us) / 1000.0
    )

    if a.settling_us is not None:
        abs_status = "SETTLED"
    elif "target_band_not_reached" in a.flags:
        abs_status = "TARGET_BAND_NOT_REACHED"
    elif "settling_not_observed" in a.flags:
        abs_status = "NOT_SETTLED"
    else:
        abs_status = "UNAVAILABLE"

    if a.observed_settling_us is not None:
        obs_status = "SETTLED"
    elif "observed_plateau_not_found" in a.flags:
        obs_status = "PLATEAU_NOT_FOUND"
    elif "observed_settling_not_observed" in a.flags:
        obs_status = "NOT_SETTLED"
    else:
        obs_status = "UNAVAILABLE"

    return {
        "transition": transition_no,
        "request_mode": a.request_mode,
        "request_message": a.request_message,
        "object_position": a.object_position,
        "request_sno": a.request_sno,
        "direction": a.direction,
        "from_V": a.baseline_vbus_V,
        "target_V": a.target_voltage_V,
        "observed_plateau_V": plateau,
        "target_minus_plateau_mV": target_minus_plateau_mv,
        "accept_ms": _ms_from_us(a.accept_latency_us),
        "movement_start_ms": _ms_from_us(a.movement_latency_us),
        "ps_rdy_ms": _ms_from_us(a.ps_rdy_latency_us),
        "vbus_at_ps_rdy_V": ps_v,
        "ps_rdy_vs_plateau_percent": ps_rdy_vs_plateau_percent,
        "ps_rdy_error_from_plateau_mV": ps_rdy_error_from_plateau_mv,
        "absolute_settle_status": abs_status,
        "absolute_settle_ms": _ms_from_us(a.settling_latency_us),
        "ps_rdy_to_absolute_settle_ms": ps_to_abs_ms,
        "absolute_slew_V_per_s": a.average_slew_V_per_s,
        "observed_settle_status": obs_status,
        "observed_settle_ms": _ms_from_us(a.observed_settling_latency_us),
        "ps_rdy_to_observed_settle_ms": ps_to_obs_ms,
        "movement_to_observed_settle_ms": move_to_obs_ms,
        "observed_slew_V_per_s": a.observed_average_slew_V_per_s,
        "flags": ";".join(a.flags),
    }

def _fmt_num(value, digits=3, suffix=""):
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}{suffix}"

def _write_human_transition_summary(
    analyses: list[AVSTransitionAnalysis],
    *,
    csv_path: Path,
    text_path: Path,
) -> None:
    rows = [
        _human_summary_row(i, a)
        for i, a in enumerate(analyses, 1)
    ]

    with csv_path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=HUMAN_TRANSITION_SUMMARY_COLUMNS,
        )
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        "CY4500-EPR PPS/AVS per-transition summary",
        "====================================",
        "",
        (
            "T#  SNo  Mode     Dir    From -> Target   Plateau   Move     PS_RDY   "
            "V@PS / Plateau   Obs.Set  PS->Obs   Obs.Slew   Abs"
        ),
        (
            "--  ---  ----  ---------------  --------  -------  -------  "
            "---------------  -------  -------  ---------  ----------------------"
        ),
    ]

    for row in rows:
        from_to = (
            f"{_fmt_num(row['from_V'], 2):>5}"
            f" -> {_fmt_num(row['target_V'], 2):>5}V"
        )
        ps_ratio = (
            "-"
            if row["ps_rdy_vs_plateau_percent"] is None
            else f"{row['vbus_at_ps_rdy_V']:.3f}V / "
                 f"{row['ps_rdy_vs_plateau_percent']:.1f}%"
        )
        abs_text = (
            row["absolute_settle_status"]
            if row["absolute_settle_ms"] is None
            else f"{row['absolute_settle_ms']:.1f}ms"
        )
        lines.append(
            f"{row['transition']:>2}  "
            f"{str(row['request_sno']):>3}  "
            f"{row['request_mode']:<7}  "
            f"{row['direction']:<4}  "
            f"{from_to:<15}  "
            f"{_fmt_num(row['observed_plateau_V'], 3, 'V'):>8}  "
            f"{_fmt_num(row['movement_start_ms'], 1, 'ms'):>7}  "
            f"{_fmt_num(row['ps_rdy_ms'], 1, 'ms'):>7}  "
            f"{ps_ratio:>15}  "
            f"{_fmt_num(row['observed_settle_ms'], 1, 'ms'):>7}  "
            f"{_fmt_num(row['ps_rdy_to_observed_settle_ms'], 1, 'ms'):>7}  "
            f"{_fmt_num(row['observed_slew_V_per_s'], 2, 'V/s'):>9}  "
            f"{abs_text}"
        )

    lines.extend(
        [
            "",
            "Column meanings:",
            "  Mode      = SPR_PPS / SPR_AVS / EPR_AVS",
            "  Move      = REQUEST/EPR_REQUEST -> sustained VBUS movement start",
            "  PS_RDY    = REQUEST/EPR_REQUEST -> PS_RDY",
            "  V@PS      = scope VBUS nearest PS_RDY",
            "  Plateau   = observed final plateau from stable scope data",
            "  Obs.Set   = REQUEST/EPR_REQUEST -> observed-plateau settle",
            "  PS->Obs   = PS_RDY -> observed-plateau settle "
            "(negative means settled before PS_RDY)",
            "  Abs       = requested-target absolute settling result",
            "",
            "Note: absolute target and observed plateau are intentionally "
            "reported separately.",
        ]
    )
    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

def _write_transition_outputs(
    analyses: list[AVSTransitionAnalysis],
    *,
    csv_path: Path,
    summary_path: Path,
    settings: dict[str, object],
    human_csv_path: Optional[Path] = None,
    human_text_path: Optional[Path] = None,
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    with csv_path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=TRANSITION_CSV_COLUMNS)
        writer.writeheader()
        for a in analyses:
            writer.writerow(_transition_row(a))

    lines = [
        "ET240 synchronized PPS/AVS transition analysis",
        "==============================================",
        "Clock handling: PD and scope device timestamps are used as captured;",
        "no host-time offset or fitted clock offset is applied.",
        "",
        "Analysis settings:",
    ]
    for key, value in settings.items():
        lines.append(f"  {key}: {value}")

    lines.extend(["", f"Transitions analyzed: {len(analyses)}", ""])

    for n, a in enumerate(analyses, 1):
        flags = ", ".join(a.flags) if a.flags else "none"
        lines.extend(
            [
                (
                    f"[{n}] SNo={a.request_sno} {a.request_mode} {a.direction} "
                    f"target={a.target_voltage_V:.3f} V "
                    f"current={a.requested_current_A if a.requested_current_A is not None else 'n/a'} A"
                ),
                f"  Request          : {a.request_start_us} us",
                (
                    f"  ACCEPT           : {a.accept_start_us} us "
                    f"(+{a.accept_latency_us/1000:.3f} ms)"
                    if a.accept_start_us is not None else
                    "  ACCEPT           : not matched"
                ),
                (
                    f"  PS_RDY           : {a.ps_rdy_start_us} us "
                    f"(+{a.ps_rdy_latency_us/1000:.3f} ms)"
                    if a.ps_rdy_start_us is not None else
                    "  PS_RDY           : not matched"
                ),
                (
                    f"  scope @ Request   : {a.request_scope_vbus_V:.6f} V "
                    f"@ {a.request_scope_timestamp_us} us"
                    if a.request_scope_vbus_V is not None else
                    "  scope @ Request   : unavailable"
                ),
                (
                    f"  scope @ PS_RDY    : {a.ps_rdy_scope_vbus_V:.6f} V "
                    f"@ {a.ps_rdy_scope_timestamp_us} us"
                    if a.ps_rdy_scope_vbus_V is not None else
                    "  scope @ PS_RDY    : unavailable"
                ),
                (
                    f"  Baseline         : {a.baseline_vbus_V:.6f} V; "
                    f"MAD={a.baseline_noise_mad_V:.6f} V; "
                    f"move threshold={a.movement_threshold_V:.6f} V"
                    if (
                        a.baseline_vbus_V is not None
                        and a.baseline_noise_mad_V is not None
                        and a.movement_threshold_V is not None
                    ) else
                    "  Baseline         : incomplete"
                ),
                (
                    f"  Movement start   : {a.movement_start_us} us "
                    f"(+{a.movement_latency_us/1000:.3f} ms)"
                    if a.movement_start_us is not None else
                    "  Movement start   : not detected"
                ),
                (
                    f"  Target crossing  : {a.target_crossing_us} us "
                    f"(+{a.target_crossing_latency_us/1000:.3f} ms)"
                    if a.target_crossing_us is not None else
                    "  Target crossing  : not observed"
                ),
                (
                    f"  ±{a.target_band_fraction*100:.2f}% first entry: "
                    f"{a.target_band_first_entry_us} us "
                    f"(+{a.target_band_first_entry_latency_us/1000:.3f} ms)"
                    if a.target_band_first_entry_us is not None else
                    f"  ±{a.target_band_fraction*100:.2f}% first entry: not observed"
                ),
                (
                    f"  Settled          : {a.settling_us} us "
                    f"(+{a.settling_latency_us/1000:.3f} ms; "
                    f"hold={a.settling_hold_us/1000:.1f} ms)"
                    if a.settling_us is not None else
                    "  Settled          : not observed"
                ),
                (
                    f"  Absolute slew    : {a.average_slew_V_per_s:.3f} V/s"
                    if a.average_slew_V_per_s is not None else
                    "  Absolute slew    : unavailable"
                ),
                (
                    f"  Observed plateau : {a.observed_plateau_V:.6f} V "
                    f"(MAD={a.observed_plateau_mad_V:.6f} V, "
                    f"n={a.observed_plateau_sample_count})"
                    if (
                        a.observed_plateau_V is not None
                        and a.observed_plateau_mad_V is not None
                    ) else
                    "  Observed plateau : unavailable"
                ),
                (
                    f"  Observed ±{a.observed_band_fraction*100:.2f}% entry: "
                    f"{a.observed_band_first_entry_us} us "
                    f"(+{a.observed_band_first_entry_latency_us/1000:.3f} ms)"
                    if a.observed_band_first_entry_us is not None else
                    f"  Observed ±{a.observed_band_fraction*100:.2f}% entry: unavailable"
                ),
                (
                    f"  Observed settled : {a.observed_settling_us} us "
                    f"(+{a.observed_settling_latency_us/1000:.3f} ms)"
                    if a.observed_settling_us is not None else
                    "  Observed settled : unavailable"
                ),
                (
                    f"  Observed slew    : {a.observed_average_slew_V_per_s:.3f} V/s"
                    if a.observed_average_slew_V_per_s is not None else
                    "  Observed slew    : unavailable"
                ),
                f"  Flags            : {flags}",
                "",
            ]
        )

    summary_path.write_text("\n".join(lines), encoding="utf-8")

    if human_csv_path is not None and human_text_path is not None:
        _write_human_transition_summary(
            analyses,
            csv_path=human_csv_path,
            text_path=human_text_path,
        )

def _run_sync_analysis(
    pd_csv: Path,
    scope_csv: Path,
    out_prefix: Path,
    *,
    baseline_window_ms: float,
    baseline_guard_ms: float,
    movement_min_threshold_v: float,
    movement_mad_multiplier: float,
    movement_sustain_samples: int,
    target_band_percent: float,
    settle_hold_ms: float,
    settle_max_gap_ms: float,
    plateau_lookback_ms: float,
    plateau_min_samples: int,
    observed_band_percent: float,
    observed_settle_hold_ms: float,
    plateau_stability_min_span_v: float,
    plateau_stability_percent: float,
    plateau_target_guard_percent: float,
) -> list[AVSTransitionAnalysis]:
    pd_samples = _load_pd_capture_csv(pd_csv)
    scope_samples = _load_scope_csv(scope_csv)

    settings = {
        "baseline_window_ms": baseline_window_ms,
        "baseline_guard_ms": baseline_guard_ms,
        "movement_min_threshold_V": movement_min_threshold_v,
        "movement_mad_multiplier": movement_mad_multiplier,
        "movement_sustain_samples": movement_sustain_samples,
        "target_band_percent": target_band_percent,
        "settle_hold_ms": settle_hold_ms,
        "settle_max_gap_ms": settle_max_gap_ms,
        "plateau_lookback_ms": plateau_lookback_ms,
        "plateau_min_samples": plateau_min_samples,
        "observed_band_percent": observed_band_percent,
        "observed_settle_hold_ms": observed_settle_hold_ms,
        "plateau_stability_min_span_V": plateau_stability_min_span_v,
        "plateau_stability_percent": plateau_stability_percent,
        "plateau_target_guard_percent": plateau_target_guard_percent,
    }

    analyses = analyze_programmable_transitions(
        pd_samples,
        scope_samples,
        baseline_window_us=int(round(baseline_window_ms * 1000.0)),
        baseline_guard_us=int(round(baseline_guard_ms * 1000.0)),
        movement_min_threshold_V=movement_min_threshold_v,
        movement_mad_multiplier=movement_mad_multiplier,
        movement_sustain_samples=movement_sustain_samples,
        target_band_fraction=target_band_percent / 100.0,
        settle_hold_us=int(round(settle_hold_ms * 1000.0)),
        settle_max_sample_gap_us=int(round(settle_max_gap_ms * 1000.0)),
        plateau_lookback_us=int(round(plateau_lookback_ms * 1000.0)),
        plateau_min_samples=int(plateau_min_samples),
        observed_band_fraction=observed_band_percent / 100.0,
        observed_settle_hold_us=int(round(observed_settle_hold_ms * 1000.0)),
        plateau_stability_min_span_V=plateau_stability_min_span_v,
        plateau_stability_fraction=plateau_stability_percent / 100.0,
        plateau_target_guard_fraction=plateau_target_guard_percent / 100.0,
    )

    transition_csv = out_prefix.with_suffix(".transitions.csv")
    transition_summary = out_prefix.with_suffix(".transitions.txt")
    human_summary_csv = out_prefix.with_suffix(".transition_summary.csv")
    human_summary_text = out_prefix.with_suffix(".transition_summary.txt")
    _write_transition_outputs(
        analyses,
        csv_path=transition_csv,
        summary_path=transition_summary,
        settings=settings,
        human_csv_path=human_summary_csv,
        human_text_path=human_summary_text,
    )
    return analyses

def _print_transition_analysis(analyses: list[AVSTransitionAnalysis]) -> None:
    print()
    print("PPS/AVS TRANSITION ANALYSIS")
    if not analyses:
        print("No SPR PPS / SPR AVS / EPR AVS requests with sufficient PDO context found.")
        return

    for a in analyses:
        ps = (
            f"{a.ps_rdy_latency_us / 1000:.3f} ms"
            if a.ps_rdy_latency_us is not None else "-"
        )
        move = (
            f"{a.movement_latency_us / 1000:.3f} ms"
            if a.movement_latency_us is not None else "-"
        )
        settle = (
            f"{a.settling_latency_us / 1000:.3f} ms"
            if a.settling_latency_us is not None else "-"
        )
        ps_v = (
            f"{a.ps_rdy_scope_vbus_V:.3f} V"
            if a.ps_rdy_scope_vbus_V is not None else "-"
        )
        abs_slew = (
            f"{a.average_slew_V_per_s:.2f} V/s"
            if a.average_slew_V_per_s is not None else "-"
        )
        obs_settle = (
            f"{a.observed_settling_latency_us / 1000:.3f} ms"
            if a.observed_settling_latency_us is not None else "-"
        )
        obs_plateau = (
            f"{a.observed_plateau_V:.3f} V"
            if a.observed_plateau_V is not None else "-"
        )
        obs_slew = (
            f"{a.observed_average_slew_V_per_s:.2f} V/s"
            if a.observed_average_slew_V_per_s is not None else "-"
        )
        print(
            f"SNo={a.request_sno!s:>3} {a.request_mode:7s} {a.direction:4s} "
            f"target={a.target_voltage_V:6.3f}V "
            f"move={move:>10} PS_RDY={ps:>10} "
            f"V@PS={ps_v:>9} abs_settle={settle:>10} "
            f"abs_slew={abs_slew:>10} | "
            f"plateau={obs_plateau:>9} "
            f"obs_settle={obs_settle:>10} "
            f"obs_slew={obs_slew:>10} "
            f"flags={';'.join(a.flags) if a.flags else '-'}"
        )

TRANSITION_OUTPUT_SUFFIXES = (
    ".transitions.csv",
    ".transitions.txt",
    ".transition_summary.csv",
    ".transition_summary.txt",
)

def _check_output_paths(paths, *, force: bool) -> None:
    if force:
        return
    existing = [str(path.resolve()) for path in paths if path.exists()]
    if existing:
        raise ValueError(
            "output already exists; choose a new --out-prefix or use --force "
            "to overwrite:\n  " + "\n  ".join(existing)
        )

def _cmd_analyze_sync(args) -> int:
    pd_csv = Path(args.pd_csv)
    scope_csv = Path(args.scope_csv)
    prefix = Path(args.out_prefix)

    _check_output_paths(
        [prefix.with_suffix(suffix) for suffix in TRANSITION_OUTPUT_SUFFIXES],
        force=args.force,
    )

    analyses = _run_sync_analysis(
        pd_csv,
        scope_csv,
        prefix,
        baseline_window_ms=args.baseline_window_ms,
        baseline_guard_ms=args.baseline_guard_ms,
        movement_min_threshold_v=args.movement_min_threshold_v,
        movement_mad_multiplier=args.movement_mad_multiplier,
        movement_sustain_samples=args.movement_sustain_samples,
        target_band_percent=args.target_band_percent,
        settle_hold_ms=args.settle_hold_ms,
        settle_max_gap_ms=args.settle_max_gap_ms,
        plateau_lookback_ms=args.plateau_lookback_ms,
        plateau_min_samples=args.plateau_min_samples,
        observed_band_percent=args.observed_band_percent,
        observed_settle_hold_ms=args.observed_settle_hold_ms,
        plateau_stability_min_span_v=args.plateau_stability_min_span_v,
        plateau_stability_percent=args.plateau_stability_percent,
        plateau_target_guard_percent=args.plateau_target_guard_percent,
    )
    _print_transition_analysis(analyses)
    print(f"Transitions CSV : {prefix.with_suffix('.transitions.csv').resolve()}")
    print(f"Transitions txt : {prefix.with_suffix('.transitions.txt').resolve()}")
    print(
        f"Human summary   : "
        f"{prefix.with_suffix('.transition_summary.csv').resolve()}"
    )
    print(
        f"Human text      : "
        f"{prefix.with_suffix('.transition_summary.txt').resolve()}"
    )
    return 0

def _add_transition_analysis_options(p) -> None:
    p.add_argument(
        "--baseline-window-ms",
        type=float,
        default=TRANSITION_BASELINE_WINDOW_US / 1000.0,
        help="pre-request baseline window length (default: 100 ms)",
    )
    p.add_argument(
        "--baseline-guard-ms",
        type=float,
        default=TRANSITION_BASELINE_GUARD_US / 1000.0,
        help="exclude this time immediately before request from baseline (default: 5 ms)",
    )
    p.add_argument(
        "--movement-min-threshold-v",
        type=float,
        default=TRANSITION_MOVEMENT_MIN_THRESHOLD_V,
        help="minimum sustained VBUS deviation used for movement detection (default: 0.05 V)",
    )
    p.add_argument(
        "--movement-mad-multiplier",
        type=float,
        default=TRANSITION_MOVEMENT_MAD_MULTIPLIER,
        help="baseline MAD multiplier for movement threshold (default: 6)",
    )
    p.add_argument(
        "--movement-sustain-samples",
        type=int,
        default=TRANSITION_MOVEMENT_SUSTAIN_SAMPLES,
        help="consecutive scope samples required for movement detection (default: 5)",
    )
    p.add_argument(
        "--target-band-percent",
        type=float,
        default=TRANSITION_TARGET_BAND_FRACTION * 100.0,
        help="target-voltage band half-width in percent (default: 1.0)",
    )
    p.add_argument(
        "--settle-hold-ms",
        type=float,
        default=TRANSITION_SETTLE_HOLD_US / 1000.0,
        help="continuous in-band duration required to declare settled (default: 20 ms)",
    )
    p.add_argument(
        "--settle-max-gap-ms",
        type=float,
        default=TRANSITION_SETTLE_MAX_SAMPLE_GAP_US / 1000.0,
        help="maximum allowed scope sample gap during settle hold (default: 5 ms)",
    )
    p.add_argument(
        "--plateau-lookback-ms",
        type=float,
        default=TRANSITION_PLATEAU_LOOKBACK_US / 1000.0,
        help="terminal scope window used to estimate observed final plateau (default: 150 ms)",
    )
    p.add_argument(
        "--plateau-min-samples",
        type=int,
        default=TRANSITION_PLATEAU_MIN_SAMPLES,
        help="minimum terminal scope samples required for observed plateau (default: 50)",
    )
    p.add_argument(
        "--observed-band-percent",
        type=float,
        default=TRANSITION_RELATIVE_BAND_FRACTION * 100.0,
        help="relative settling band around observed plateau (default: 0.5%%)",
    )
    p.add_argument(
        "--observed-settle-hold-ms",
        type=float,
        default=TRANSITION_RELATIVE_SETTLE_HOLD_US / 1000.0,
        help="continuous observed-band hold time (default: 20 ms)",
    )
    p.add_argument(
        "--plateau-stability-min-span-v",
        type=float,
        default=TRANSITION_PLATEAU_STABILITY_MIN_SPAN_V,
        help="minimum allowed VBUS-span limit for plateau window (default: 0.10 V)",
    )
    p.add_argument(
        "--plateau-stability-percent",
        type=float,
        default=TRANSITION_PLATEAU_STABILITY_FRACTION * 100.0,
        help="relative allowed VBUS span for plateau window (default: 0.25%%)",
    )
    p.add_argument(
        "--plateau-target-guard-percent",
        type=float,
        default=TRANSITION_PLATEAU_TARGET_GUARD_FRACTION * 100.0,
        help="plausibility guard around requested target for observed plateau selection (default: 10%%)",
    )
