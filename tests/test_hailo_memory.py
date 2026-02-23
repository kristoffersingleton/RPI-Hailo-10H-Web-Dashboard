#!/usr/bin/env python3
"""
test_hailo_memory.py
====================
Documents and validates the behaviour of query_performance_stats()
ram_size_used / ram_size_total during Hailo-10H inference.

Hypothesis (naive expectation):
    Loading a ~25 MB model (yolov6n.hef) and running inference should
    cause ram_size_used to increase substantially — ideally tracking
    how much of the device's memory budget is consumed.

Actual finding:
    ram_size_used stays essentially flat (~130 KB) before, during, and
    after inference. The SoC OS heap tracked by query_performance_stats
    does not cover the DMA-mapped regions used for model weights and
    activations. See session_notes.md for full context.

Uses:
    - hailo_platform: direct VDevice + InferVStreams (no GStreamer)
    - hailo_perf_query: C++ binary wrapping query_performance_stats /
      query_health_stats (see ../hailo_perf_query.cpp)
    - Model:  ~/code/hailo-apps/resources/models/hailo10h/yolov6n.hef
    - Images: ~/code/hailo-apps/resources/images/*.jpg

Run:
    # sentinel must NOT be running (it holds /dev/hailo0 exclusively)
    sudo systemctl stop sentinel.service
    source ~/code/hailo-apps/venv_hailo_apps/bin/activate
    python tests/test_hailo_memory.py
    sudo systemctl start sentinel.service
"""

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
HEF_PATH       = HAILO_APPS / "resources/models/hailo10h/yolov6n.hef"
IMAGES_DIR     = HAILO_APPS / "resources/images"
PERF_QUERY_BIN = Path(__file__).resolve().parents[1] / "hailo_perf_query"

INFERENCE_FRAMES = 50    # frames to push through the network
POLL_INTERVAL_S  = 0.5   # seconds between perf polls during inference


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
        for p in pids:
            comm = Path(f"/proc/{p}/comm")
            names.append(comm.read_text().strip() if comm.exists() else p)
        print(f"\n❌  /dev/hailo0 is held by: {', '.join(names)} (PID {', '.join(pids)})")
        print("    Stop sentinel first:  sudo systemctl stop sentinel.service")
        sys.exit(1)


def load_sample_frames(shape):
    """Load JPEG images from hailo-apps resources, resize to model input shape.
    Falls back to random noise frames if no images found."""
    h, w, c = shape
    frames = []
    for p in sorted(IMAGES_DIR.glob("*.jpg"))[:5]:
        try:
            import cv2
            img = cv2.imread(str(p))
            img = cv2.resize(img, (w, h))
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            frames.append(img.astype(np.uint8))
        except Exception:
            pass
    if not frames:
        print("  [warn] No images found / cv2 unavailable — using random frames")
        frames = [np.random.randint(0, 255, (h, w, c), dtype=np.uint8) for _ in range(5)]
    return frames


def print_row(label, val, expected=None):
    indicator = ""
    if expected is not None:
        indicator = "  ✓ matches" if val == expected else f"  ← expected ~{fmt_bytes(expected)}"
    print(f"  {label:<30} {fmt_bytes(val)}{indicator}")


# ── Main test ─────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("  Hailo-10H memory test: query_performance_stats ram_size_*")
    print("=" * 65)

    # Pre-flight checks
    check_device_free()
    assert PERF_QUERY_BIN.exists(), f"hailo_perf_query not found at {PERF_QUERY_BIN}"
    assert HEF_PATH.exists(),       f"HEF not found at {HEF_PATH}"

    # Import here so errors are visible before we start the test
    from hailo_platform import VDevice, HEF, HailoSchedulingAlgorithm

    # ── Stage 1: Baseline (no model loaded) ───────────────────────────────────
    print("\n[1] Baseline — device idle, no model loaded")
    p_idle = perf_query()
    idle_used  = p_idle.get("ram_size_used")
    idle_total = p_idle.get("ram_size_total")
    print_row("ram_size_used",  idle_used)
    print_row("ram_size_total", idle_total)
    print(f"  cpu_utilization:               {p_idle.get('cpu_utilization', 'N/A'):.1f}%")

    # What we'd naively expect after loading a ~25 MB model:
    hef_size_bytes = HEF_PATH.stat().st_size
    expected_used_after_load = (idle_used or 0) + hef_size_bytes
    print(f"\n  HEF file size:                 {fmt_bytes(hef_size_bytes)}")
    print(f"  Naive expectation after load:  {fmt_bytes(expected_used_after_load)}")
    print(f"  (model weights + activations would be even larger at runtime)")

    # ── Stage 2: Load model ───────────────────────────────────────────────────
    # Uses the InferModel API (hailo_platform v5+, works on Hailo-10H).
    # The older VDevice.configure() + InferVStreams API returns
    # HAILO_NOT_IMPLEMENTED on Hailo-10H SoC mode.
    print("\n[2] Loading model — yolov6n.hef via hailo_platform InferModel API")
    params = VDevice.create_params()
    params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
    target      = VDevice(params)
    infer_model = target.create_infer_model(str(HEF_PATH))
    infer_model.set_batch_size(1)
    input_shape = infer_model.input().shape   # (640, 640, 3)
    print(f"  Input shape:  {input_shape}")
    print(f"  Outputs:      {[o.name for o in infer_model.outputs]}")

    time.sleep(0.5)  # let firmware settle after model load
    p_loaded = perf_query()
    loaded_used = p_loaded.get("ram_size_used")
    print("\n  After model load:")
    print_row("ram_size_used",  loaded_used, expected=expected_used_after_load)
    print_row("ram_size_total", p_loaded.get("ram_size_total"))
    delta_load = (loaded_used or 0) - (idle_used or 0)
    print(f"  Delta from idle:               {fmt_bytes(delta_load)}")

    # ── Stage 3: Active inference ─────────────────────────────────────────────
    print(f"\n[3] Running {INFERENCE_FRAMES} inference frames")
    frames = load_sample_frames(input_shape)
    samples_during = []

    with infer_model.configure() as configured:
        output_buffers = {o.name: np.empty(o.shape, dtype=np.float32)
                         for o in infer_model.outputs}
        for i in range(INFERENCE_FRAMES):
            frame = frames[i % len(frames)]
            bindings = configured.create_bindings()
            bindings.input().set_buffer(frame)
            for name, buf in output_buffers.items():
                bindings.output(name).set_buffer(buf)
            job = configured.run_async([bindings])
            job.wait(2000)

            if i % 10 == 0:
                p = perf_query()
                u = p.get("ram_size_used")
                samples_during.append(u)
                print(f"  frame {i:>3}  ram_size_used={fmt_bytes(u)}"
                      f"  nnc={p.get('nnc_utilization', '?')}%"
                      f"  cpu={p.get('cpu_utilization', '?'):.1f}%")

    # ── Stage 4: After unload ─────────────────────────────────────────────────
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
    print(f"  HEF model size:                {fmt_bytes(hef_size_bytes)}")
    print(f"  Naive expected increase:       ≥{fmt_bytes(hef_size_bytes)} (weights alone)")
    print()
    print(f"  ram_size_used @ idle:          {fmt_bytes(idle_used)}")
    print(f"  ram_size_used @ model loaded:  {fmt_bytes(loaded_used)}  (delta: {fmt_bytes(delta_load)})")
    if samples_during:
        mn, mx = min(s for s in samples_during if s), max(s for s in samples_during if s)
        print(f"  ram_size_used @ inference:     {fmt_bytes(mn)}–{fmt_bytes(mx)}")
    print(f"  ram_size_used @ after release: {fmt_bytes(after_used)}")
    print()
    print("  CONCLUSION")
    print("  ram_size_* tracks the Hailo SoC firmware's OS heap only.")
    print("  Model weights and inference buffers are DMA-mapped directly")
    print("  into hardware memory — invisible to query_performance_stats.")
    print("  This counter will not reflect actual NPU memory utilisation.")
    print("=" * 65)


if __name__ == "__main__":
    main()
