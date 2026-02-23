#!/usr/bin/env python3
"""
test_hailo_memory.py
====================
Documents and validates the behaviour of query_performance_stats()
ram_size_used / ram_size_total during Hailo-10H inference.

Hypothesis (naive expectation):
    Loading a model and running inference should cause ram_size_used to
    increase substantially — ideally tracking how much of the device's
    memory budget is consumed.

Actual finding:
    ram_size_used stays essentially flat (~120-130 KB) before, during, and
    after inference regardless of model size. The SoC OS heap tracked by
    query_performance_stats does not cover the DMA-mapped regions used for
    model weights and activations. See session_notes.md for full context.

Uses:
    - hailo_platform InferModel API (VDevice.configure()+InferVStreams returns
      HAILO_NOT_IMPLEMENTED on Hailo-10H; InferModel API is the correct path)
    - hailo_perf_query: C++ binary wrapping query_performance_stats /
      query_health_stats (see ../hailo_perf_query.cpp)
    - Models: ~/code/hailo-apps/resources/models/hailo10h/
    - Images: ~/code/hailo-apps/resources/images/*.jpg (used as input frames
      where shape matches; random noise used for non-image inputs)

Run:
    # sentinel must NOT be running (it holds /dev/hailo0 exclusively)
    sudo systemctl stop sentinel.service
    source ~/code/hailo-apps/venv_hailo_apps/bin/activate

    python tests/test_hailo_memory.py                          # default: yolov6n
    python tests/test_hailo_memory.py --list                   # show available models
    python tests/test_hailo_memory.py --hef yolov8m            # by name
    python tests/test_hailo_memory.py --hef yolov8m --frames 20
    python tests/test_hailo_memory.py --hef /full/path/to/model.hef

    sudo systemctl start sentinel.service
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

# ── Paths ─────────────────────────────────────────────────────────────────────

REPO_ROOT      = Path(__file__).resolve().parents[3]   # ~/code
HAILO_APPS     = REPO_ROOT / "hailo-apps"
MODELS_DIR     = HAILO_APPS / "resources/models/hailo10h"
IMAGES_DIR     = HAILO_APPS / "resources/images"
PERF_QUERY_BIN = Path(__file__).resolve().parents[1] / "hailo_perf_query"


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--hef", default="yolov6n",
        help="HEF model name (without .hef) or full path. Default: yolov6n"
    )
    p.add_argument(
        "--frames", type=int, default=50,
        help="Number of inference frames to run. Default: 50"
    )
    p.add_argument(
        "--list", action="store_true",
        help="List available hailo10h models and exit"
    )
    return p.parse_args()


def list_models():
    print(f"\nAvailable models in {MODELS_DIR}:\n")
    models = sorted(MODELS_DIR.glob("*.hef"), key=lambda p: p.stat().st_size)
    for m in models:
        size = m.stat().st_size
        print(f"  {fmt_bytes(size):>10}  {m.stem}")
    print()


def resolve_hef(hef_arg):
    p = Path(hef_arg)
    if p.suffix == ".hef" and p.exists():
        return p
    # try as a name in the models dir
    candidate = MODELS_DIR / (hef_arg if hef_arg.endswith(".hef") else hef_arg + ".hef")
    if candidate.exists():
        return candidate
    print(f"\n❌  HEF not found: {hef_arg}")
    print(f"    Run with --list to see available models.")
    sys.exit(1)


# ── Helpers ───────────────────────────────────────────────────────────────────

def perf_query():
    """Call hailo_perf_query binary and return parsed dict, or {} on error."""
    try:
        r = subprocess.run([str(PERF_QUERY_BIN)], capture_output=True,
                           text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception as e:
        print(f"  [warn] hailo_perf_query failed: {e}")
    return {}


def fmt_bytes(n):
    if n is None or n < 0:
        return "N/A"
    if n >= 1_073_741_824:
        return f"{n / 1_073_741_824:.2f} GB"
    if n >= 1_048_576:
        return f"{n / 1_048_576:.2f} MB"
    return f"{n / 1024:.1f} KB"


def check_device_free():
    """Abort if /dev/hailo0 is already held by another process."""
    result = subprocess.run(
        ["lsof", "-F", "p", "/dev/hailo0"],
        capture_output=True, text=True
    )
    pids = [l[1:] for l in result.stdout.splitlines()
            if l.startswith("p") and l[1:] != str(os.getpid())]
    if pids:
        names = []
        for pid in pids:
            comm = Path(f"/proc/{pid}/comm")
            names.append(comm.read_text().strip() if comm.exists() else pid)
        print(f"\n❌  /dev/hailo0 is held by: {', '.join(names)} (PID {', '.join(pids)})")
        print("    Stop sentinel first:  sudo systemctl stop sentinel.service")
        sys.exit(1)


def make_input_frame(shape, dtype=np.uint8):
    """Return a single frame of the given shape.
    For 3-channel image shapes, tries to load a real image first.
    Falls back to random noise for all other shapes/dtypes."""
    if len(shape) == 3 and shape[2] == 3 and dtype == np.uint8:
        h, w = shape[0], shape[1]
        for p in sorted(IMAGES_DIR.glob("*.jpg"))[:1]:
            try:
                import cv2
                img = cv2.imread(str(p))
                img = cv2.resize(img, (w, h))
                return cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.uint8)
            except Exception:
                pass
    # Random noise for non-image inputs (e.g. token IDs, masks, embeddings)
    if np.issubdtype(dtype, np.integer):
        return np.random.randint(0, 128, shape, dtype=dtype)
    return np.random.rand(*shape).astype(dtype)


def print_row(label, val, expected=None):
    indicator = ""
    if expected is not None:
        indicator = "  ✓ matches" if val == expected else f"  ← expected ~{fmt_bytes(expected)}"
    print(f"  {label:<32} {fmt_bytes(val)}{indicator}")


# ── Main test ─────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    if args.list:
        list_models()
        return

    hef_path = resolve_hef(args.hef)

    print("=" * 65)
    print("  Hailo-10H memory test: query_performance_stats ram_size_*")
    print(f"  Model:  {hef_path.name}")
    print(f"  Frames: {args.frames}")
    print("=" * 65)

    check_device_free()
    assert PERF_QUERY_BIN.exists(), f"hailo_perf_query not found at {PERF_QUERY_BIN}"

    from hailo_platform import VDevice, HailoSchedulingAlgorithm

    # ── Stage 1: Baseline ─────────────────────────────────────────────────────
    print("\n[1] Baseline — device idle, no model loaded")
    p_idle = perf_query()
    idle_used  = p_idle.get("ram_size_used")
    idle_total = p_idle.get("ram_size_total")
    print_row("ram_size_used",  idle_used)
    print_row("ram_size_total", idle_total)
    print(f"  cpu_utilization:                {p_idle.get('cpu_utilization', 'N/A'):.1f}%")

    hef_size_bytes = hef_path.stat().st_size
    expected_used_after_load = (idle_used or 0) + hef_size_bytes
    print(f"\n  HEF file size:                  {fmt_bytes(hef_size_bytes)}")
    print(f"  Naive expectation after load:   {fmt_bytes(expected_used_after_load)}")
    print(f"  (activations at runtime would be larger still)")

    # ── Stage 2: Load model ───────────────────────────────────────────────────
    print(f"\n[2] Loading {hef_path.name} via hailo_platform InferModel API")
    if hef_size_bytes > 500_000_000:
        print("  [note] Large model — load may take 30-60 seconds…")

    params = VDevice.create_params()
    params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
    target      = VDevice(params)
    infer_model = target.create_infer_model(str(hef_path))
    infer_model.set_batch_size(1)

    inputs  = infer_model.inputs
    outputs = infer_model.outputs
    print(f"  Inputs:  {[(i.name, list(i.shape)) for i in inputs]}")
    print(f"  Outputs: {[o.name for o in outputs]}")

    time.sleep(0.5)
    p_loaded = perf_query()
    loaded_used = p_loaded.get("ram_size_used")
    print("\n  After model load:")
    print_row("ram_size_used",  loaded_used, expected=expected_used_after_load)
    print_row("ram_size_total", p_loaded.get("ram_size_total"))
    delta_load = (loaded_used or 0) - (idle_used or 0)
    print(f"  Delta from idle:                {fmt_bytes(delta_load)}")

    # ── Stage 3: Active inference ─────────────────────────────────────────────
    print(f"\n[3] Running {args.frames} inference frames")
    input_frames = {i.name: make_input_frame(list(i.shape), np.uint8)
                    for i in inputs}
    output_buffers = {o.name: np.empty(o.shape, dtype=np.float32)
                      for o in outputs}
    samples_during = []

    poll_every = max(1, args.frames // 5)   # ~5 samples across the run

    with infer_model.configure() as configured:
        for i in range(args.frames):
            bindings = configured.create_bindings()
            for name, frame in input_frames.items():
                bindings.input(name).set_buffer(frame)
            for name, buf in output_buffers.items():
                bindings.output(name).set_buffer(buf)
            job = configured.run_async([bindings])
            job.wait(10_000)    # 10 s timeout — large models are slow

            if i % poll_every == 0:
                p = perf_query()
                u = p.get("ram_size_used")
                samples_during.append(u)
                print(f"  frame {i:>3}  ram_size_used={fmt_bytes(u)}"
                      f"  nnc={p.get('nnc_utilization', '?')}%"
                      f"  cpu={p.get('cpu_utilization', '?'):.1f}%")

    # ── Stage 4: After release ────────────────────────────────────────────────
    print("\n[4] Releasing VDevice")
    del infer_model, target
    time.sleep(0.5)
    p_after = perf_query()
    after_used = p_after.get("ram_size_used")
    print_row("ram_size_used after release", after_used)

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("  RESULTS SUMMARY")
    print("=" * 65)
    print(f"  Model:                          {hef_path.name}")
    print(f"  HEF file size:                  {fmt_bytes(hef_size_bytes)}")
    print(f"  Naive expected increase:        ≥{fmt_bytes(hef_size_bytes)} (weights alone)")
    print()
    print(f"  ram_size_used @ idle:           {fmt_bytes(idle_used)}")
    print(f"  ram_size_used @ model loaded:   {fmt_bytes(loaded_used)}  (delta: {fmt_bytes(delta_load)})")
    if samples_during:
        valid = [s for s in samples_during if s is not None]
        print(f"  ram_size_used @ inference:      {fmt_bytes(min(valid))}–{fmt_bytes(max(valid))}")
    print(f"  ram_size_used @ after release:  {fmt_bytes(after_used)}")
    print()
    print("  CONCLUSION")
    print("  ram_size_* tracks the Hailo SoC firmware's OS heap only.")
    print("  Model weights and inference buffers are DMA-mapped directly")
    print("  into hardware memory — invisible to query_performance_stats.")
    print("  This counter will not reflect actual NPU memory utilisation.")
    print("=" * 65)


if __name__ == "__main__":
    main()
