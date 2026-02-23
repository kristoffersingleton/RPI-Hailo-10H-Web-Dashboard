/**
 * hailo_perf_query.cpp
 *
 * Calls Device::query_performance_stats() and Device::query_health_stats()
 * and prints the result as JSON on stdout.
 *
 * Supported on Hailo-10/Hailo-15 only.
 * Safe to run alongside running inference (same as hailortcli fw-control identify).
 *
 * Build: g++ -std=c++17 -O2 -o hailo_perf_query hailo_perf_query.cpp -lhailort
 */

#include <hailo/hailort.hpp>
#include <cstdio>
#include <cstring>

using namespace hailort;

int main() {
    // Scan for devices
    auto scan_result = Device::scan();
    if (!scan_result || scan_result->empty()) {
        fprintf(stderr, "hailo_perf_query: no devices found\n");
        return 1;
    }

    // Open the first device
    auto device = Device::create(scan_result->at(0));
    if (!device) {
        fprintf(stderr, "hailo_perf_query: failed to open device (status=%d)\n",
                (int)device.status());
        return 1;
    }

    auto perf   = device.value()->query_performance_stats();
    auto health = device.value()->query_health_stats();

    printf("{\n");

    // Performance stats
    if (perf) {
        printf("  \"cpu_utilization\": %g,\n",     (double)perf->cpu_utilization);
        printf("  \"ram_size_total\": %lld,\n",    (long long)perf->ram_size_total);
        printf("  \"ram_size_used\": %lld,\n",     (long long)perf->ram_size_used);
        printf("  \"nnc_utilization\": %g,\n",     (double)perf->nnc_utilization);
        printf("  \"dsp_utilization\": %d,\n",     perf->dsp_utilization);
        printf("  \"perf_ok\": true,\n");
    } else {
        printf("  \"perf_ok\": false,\n");
        printf("  \"perf_error\": %d,\n", (int)perf.status());
    }

    // Health stats
    if (health) {
        printf("  \"on_die_temperature\": %g,\n",  (double)health->on_die_temperature);
        printf("  \"on_die_voltage\": %d,\n",       health->on_die_voltage);
        printf("  \"bist_failure_mask\": %d,\n",    health->bist_failure_mask);
        printf("  \"health_ok\": true\n");
    } else {
        printf("  \"health_ok\": false,\n");
        printf("  \"health_error\": %d\n", (int)health.status());
    }

    printf("}\n");
    return 0;
}
