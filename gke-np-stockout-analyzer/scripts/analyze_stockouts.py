#!/usr/bin/env python3
import subprocess
import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
import argparse
import sys

def get_token():
    try:
        return subprocess.check_output(['gcloud', 'auth', 'print-access-token']).decode('utf-8').strip()
    except Exception as e:
        print(f"Error getting gcloud token: {e}")
        sys.exit(1)


def query_promql(token, project_id, query, time_str=None):
    params = {"query": query}
    if time_str:
        params["time"] = time_str

    url = (
        f"https://monitoring.googleapis.com/v1/projects/{project_id}/location/global/prometheus/api/v1/query?"
        + urllib.parse.urlencode(params)
    )
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
            if data.get("status") == "success":
                result = data.get("data", {}).get("result", [])
                if result:
                    return int(float(result[0]["value"][1]))
    except Exception as e:
        pass
    return None


# Note: The compute.googleapis.com/reservation/used metric has an ingest/sampling cadence
# of approximately 30 minutes inside GCM. Therefore, we use a 60m PromQL range window (e.g., [60m])
# to guarantee that the metric evaluator finds a continuous query point at any error timestamp.
def get_reservation_used(token, project, reservation_id, time_str):
    query = f'sum(avg_over_time({{"__name__"="compute.googleapis.com/reservation/used","monitored_resource"="compute.googleapis.com/Reservation","reservation_id"="{reservation_id}"}}[60m]))'
    return query_promql(token, project, query, time_str)


def get_scheduled_chips(token, project, reservation_id, time_str):
    # TPU scheduled chips metrics are fast, but scrape/ingest lags in GCM can exceed 5m.
    # Using a 15m window guarantees scrape coverage without diluting temporal accuracy with old values.
    query = f'sum(avg_over_time({{"__name__"="compute.googleapis.com/instance/tpu/scheduled_chips","monitored_resource"="gce_instance","reservation_id"="{reservation_id}"}}[15m]))'
    return query_promql(token, project, query, time_str)


def get_reservation_limits(token, project, reservation_id, time_str):
    query = f'sum(avg_over_time({{"__name__"="compute.googleapis.com/reservation/reserved","monitored_resource"="compute.googleapis.com/Reservation","reservation_id"="{reservation_id}"}}[60m]))'
    return query_promql(token, project, query, time_str)


def run_gcloud_log(query, project):
    try:
        output = subprocess.check_output(
            [
                "gcloud",
                "logging",
                "read",
                query,
                "--freshness=7d",
                "--format=json",
                f"--project={project}",
            ]
        )
        return json.loads(output.decode('utf-8'))
    except Exception as e:
        print(f"Error running gcloud logging read: {e}")
        return []


def get_reservation_details(project, zone, reservation_name):
    try:
        output = subprocess.check_output(['gcloud', 'compute', 'reservations', 'describe', reservation_name, f'--zone={zone}', f'--project={project}', '--format=json'])
        data = json.loads(output.decode('utf-8'))

        # Find total reserved capacity
        total_reserved = 0
        ar = data.get("aggregateReservation", {})
        for rr in ar.get("reservedResources", []):
            total_reserved += rr.get("accelerator", {}).get("acceleratorCount", 0)

        return {"id": data.get("id"), "total_reserved": total_reserved}
    except Exception as e:
        print(f"Error getting reservation details for {reservation_name}: {e}")
        return None


def get_nodepool_reservation(token, project, cluster, nodepool_name, location):
    # 1. Query the GKE CreateNodePool activity logs for this specific nodepool
    query = (
        f'resource.type="gke_nodepool" AND '
        f'resource.labels.nodepool_name="{nodepool_name}" AND '
        f'protoPayload.methodName="google.container.v1beta1.ClusterManager.CreateNodePool"'
    )
    if cluster:
        query = f'resource.labels.cluster_name="{cluster}" AND ' + query

    logs = run_gcloud_log(query, project)

    if not logs:
        # Fallback to wider search if needed
        query = (
            f'logName:"logs/cloudaudit.googleapis.com%2Factivity" AND '
            f'resource.labels.nodepool_name="{nodepool_name}" AND '
            f'protoPayload.methodName="google.container.v1beta1.ClusterManager.CreateNodePool"'
        )
        logs = run_gcloud_log(query, project)

    if logs:
        logs_sorted = sorted(logs, key=lambda x: x.get("timestamp"))
        # Look for request config payload in CreateNodePool
        for l in logs_sorted:
            req = l.get("protoPayload", {}).get("request", {}).get("nodePool", {})
            res_affinity = req.get("config", {}).get("reservationAffinity", {})
            if res_affinity.get("consumeReservationType") == "SPECIFIC_RESERVATION":
                values = res_affinity.get("values", [])
                if values:
                    reservation_name = values[0]
                    zone = req.get("locations", [location])[0]
                    return get_reservation_details(project, zone, reservation_name)
    return None


def main():
    parser = argparse.ArgumentParser(description="Analyze GKE nodepool stockouts.")
    parser.add_argument("--project", required=True, help="GCP project ID")
    parser.add_argument("--cluster", help="GKE cluster name (optional)")
    parser.add_argument(
        "--nodepool-regex",
        required=True,
        help="Nodepool name regex (e.g., 'np-prefix.*')",
    )
    parser.add_argument("--output-file", help="Path to save markdown table output")

    args = parser.parse_args()
    token = get_token()

    if args.cluster:
        print(
            f"Fetching stockout errors for cluster {args.cluster} in project {args.project}..."
        )
    else:
        print(f"Fetching stockout errors in project {args.project}...")

    log_query = (
        f'resource.type="gke_nodepool" AND '
        f'resource.labels.nodepool_name=~"{args.nodepool_regex}" AND '
        f'(":[GCE_STOCKOUT]" OR "does not have enough resources" OR "cannot be created now due to lack of capacity in your reservation")'
    )
    if args.cluster:
        log_query = f'resource.labels.cluster_name="{args.cluster}" AND ' + log_query

    error_logs = run_gcloud_log(log_query, args.project)

    # Cache nodepool reservation info
    nodepool_cache = {}

    results = []
    for log in error_logs:
        msg = log.get('protoPayload', {}).get('status', {}).get('message', '')
        text_payload = log.get("textPayload", "")
        json_payload = log.get("jsonPayload", "")

        full_message = msg or text_payload or str(json_payload)

        ts_str = log.get('timestamp')
        nodepool_name = log.get('resource', {}).get('labels', {}).get('nodepool_name')
        location = log.get("resource", {}).get("labels", {}).get("location")

        if nodepool_name not in nodepool_cache:
            print(f"Resolving reservation ID for nodepool {nodepool_name}...")
            nodepool_cache[nodepool_name] = get_nodepool_reservation(
                token, args.project, args.cluster, nodepool_name, location
            )

        res_info = nodepool_cache.get(nodepool_name)

        reservation_id = None
        total_reserved = None
        if res_info:
            reservation_id = res_info.get("id")
            total_reserved = res_info.get("total_reserved")

        if not reservation_id:
            # If we cannot find specific reservation info, skip fetching GCM metrics
            results.append(
                {
                    "timestamp": ts_str,
                    "nodepool": nodepool_name,
                    "reservation_used": "Unknown",
                    "tpu_scheduled_chips": "Unknown",
                    "message": full_message.replace("\n", " ").strip(),
                }
            )
            continue

        # 1. Retrieve PromQL metrics for this exact timestamp
        used_chips = get_reservation_used(token, args.project, reservation_id, ts_str)
        scheduled_chips = get_scheduled_chips(
            token, args.project, reservation_id, ts_str
        )

        if total_reserved is None:
            total_reserved = get_reservation_limits(
                token, args.project, reservation_id, ts_str
            )

        # Format reservation/used column
        if used_chips is not None and total_reserved is not None:
            # Cap utilized value at the limit to handle GCM PromQL mathematical/averaging overlap artifacts gracefully
            display_used = min(used_chips, total_reserved)
            res_used_str = f"{display_used:,} / {total_reserved:,}"
        elif used_chips is not None:
            res_used_str = f"{used_chips:,} used"
        else:
            res_used_str = "Unknown"

        # Format tpu/scheduled_chips column
        if scheduled_chips is not None and total_reserved is not None:
            # Cap scheduled values at the limit to handle GCM PromQL windowing overlap gracefully
            display_scheduled = min(scheduled_chips, total_reserved)
            sched_chips_str = f"{display_scheduled:,} / {total_reserved:,}"
        elif scheduled_chips is not None:
            sched_chips_str = f"{scheduled_chips:,} scheduled"
        elif total_reserved is not None:
            sched_chips_str = f"0 / {total_reserved:,}"
        else:
            sched_chips_str = "Unknown"

        results.append(
            {
                "timestamp": ts_str,
                "nodepool": nodepool_name,
                "reservation_used": res_used_str,
                "tpu_scheduled_chips": sched_chips_str,
                "message": full_message.replace("\n", " ").strip(),
            }
        )

    results.sort(key=lambda x: x['timestamp'])

    # Build Markdown table
    markdown_lines = []
    markdown_lines.append(
        "| timestamp (UTC) | nodepool | reservation/used | tpu/scheduled_chips | message |"
    )
    markdown_lines.append("| :--- | :--- | :--- | :--- | :--- |")

    for r in results:
        markdown_lines.append(
            f"| {r['timestamp']} | {r['nodepool']} | {r['reservation_used']} | {r['tpu_scheduled_chips']} | {r['message']} |"
        )

    table_content = "\n".join(markdown_lines)

    print("\n--- GKE Nodepool Stockout Analysis Report ---")
    if not results:
        print("No stockout events found matching the criteria.")
    else:
        print(table_content)

    if args.output_file and results:
        try:
            with open(args.output_file, "w") as f:
                f.write(table_content)
            print(f"\nSuccessfully saved report to: {args.output_file}")
        except Exception as e:
            print(f"Error writing to output file: {e}")

if __name__ == "__main__":
    main()
