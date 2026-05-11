---
name: gke-np-stockout-analyzer
description: Analyzes GKE nodepool stockout errors to determine if they are expected (insufficient capacity) or unexpected (fragmentation or race condition). Use this when investigating GKE nodepool creation failures due to lack of capacity.
---
# GKE Nodepool Stockout Analyzer

This skill helps investigate GKE nodepool creation failures caused by a "lack of capacity" in a compute reservation. It analyzes GKE logs and reservation metrics to generate a clean markdown report showing exact timestamps, reservation usage, and error messages.

## Usage

Use the bundled `analyze_stockouts.py` script to run the analysis. It queries GKE audit logs for node pool creation failures (including repair and automatic retry events) and Cloud Monitoring for reservation capacity and scheduled chips.

**Command:**
```bash
python3 gke-np-stockout-analyzer/scripts/analyze_stockouts.py --project <PROJECT_ID> --nodepool-regex <NODEPOOL_REGEX> [--cluster <CLUSTER_NAME>] [--output-file <REPORT_PATH>]
```

**Example:**
```bash
python3 gke-np-stockout-analyzer/scripts/analyze_stockouts.py --project my-example-project --nodepool-regex "np-prefix-.*" --output-file report.md
```

## How It Works

1. **Find Errors**: It searches Cloud Audit logs for `CREATE_NODE_POOL` or `RepairNodePool` operations that failed with GCE stockout/capacity constraints.
2. **Resolve Nodepool Reservation Config**: It locates the original creation logs for matching nodepools to cache their target reservation ID and zone configuration.
3. **Query Time-Series Metrics**: It queries GCM for `compute.googleapis.com/instance/tpu/scheduled_chips` filtered by the target `reservation_id` at each error timestamp to calculate the exact chips in use versus the total reserved chips.
4. **Report Generation**: Produces a beautiful, clean Markdown table mapping each stockout event to reservation utilization and the detailed error message.

