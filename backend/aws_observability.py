"""
AWS Observability Module — Extended Edition
Full CloudWatch metrics, Security Hub, Cost Intelligence, Multi-service coverage.
Real-time metrics streaming via WebSocket to WebGL frontend.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any, Optional
from collections import defaultdict

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from auth import decode_token
from tenancy import _validate_tenant_id, require_context, require_role, Role, TenantContext

# Roles allowed to read AWS infra observability (mirrors the REST route gates).
_OBS_ALLOWED_ROLES = {"platform_admin", "broker_owner"}

log = logging.getLogger("oracle.aws_observability")

router = APIRouter(prefix="/api/aws", tags=["aws-observability"])

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
_MAX_ASG_DESIRED_CAPACITY = int(os.getenv("AWS_OBS_MAX_ASG_DESIRED_CAPACITY", "100"))
_ASG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:=+@-]{0,254}$")
_EC2_INSTANCE_RE = re.compile(r"^i-[0-9a-f]{8,17}$")
_COPILOT_CONTEXT_MAX_BYTES = 32 * 1024
_COPILOT_RATE_LIMIT = int(os.getenv("AWS_OBS_COPILOT_RATE_LIMIT", "20"))
_COPILOT_RATE_WINDOW = 60.0
_copilot_timestamps: dict[str, list[float]] = defaultdict(list)
_WS_MAX_MESSAGE_BYTES = 4096
_WS_SNAPSHOT_COOLDOWN = 5.0
_WS_METRICS_COOLDOWN = 1.0
_WS_RESOURCE_ID_PATTERNS = {
    "ec2": _EC2_INSTANCE_RE,
    "rds": re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,62}$"),
    "lambda": re.compile(r"^[A-Za-z0-9_-]{1,64}$"),
    "ebs": re.compile(r"^vol-[0-9a-f]{8,17}$"),
}

_boto_config = Config(
    region_name=AWS_REGION,
    retries={"max_attempts": 3, "mode": "adaptive"},
)


class CopilotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    context: dict[str, Any] = Field(default_factory=dict)


class ScaleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    desired_capacity: int = Field(ge=0, le=_MAX_ASG_DESIRED_CAPACITY)


def _check_copilot_rate_limit(ctx: TenantContext) -> None:
    key = f"{ctx.tenant_id}:{ctx.agent_id}"
    now = time.monotonic()
    recent = [stamp for stamp in _copilot_timestamps[key] if now - stamp < _COPILOT_RATE_WINDOW]
    if len(recent) >= _COPILOT_RATE_LIMIT:
        _copilot_timestamps[key] = recent
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Copilot rate limit exceeded.")
    recent.append(now)
    _copilot_timestamps[key] = recent


# boto3 clients are thread-safe and expensive to construct (each call re-parses
# the service model). Cache one per service — the hot broadcast loop builds 20+
# per cycle otherwise. Creds are resolved lazily per request, so IAM-role
# rotation still works with a cached client.
@lru_cache(maxsize=None)
def _get_cloudwatch_client():
    return boto3.client("cloudwatch", config=_boto_config)


@lru_cache(maxsize=None)
def _get_ec2_client():
    return boto3.client("ec2", config=_boto_config)


@lru_cache(maxsize=None)
def _get_rds_client():
    return boto3.client("rds", config=_boto_config)


@lru_cache(maxsize=None)
def _get_s3_client():
    return boto3.client("s3", config=_boto_config)


@lru_cache(maxsize=None)
def _get_lambda_client():
    return boto3.client("lambda", config=_boto_config)


@lru_cache(maxsize=None)
def _get_elbv2_client():
    return boto3.client("elbv2", config=_boto_config)


@lru_cache(maxsize=None)
def _get_cloudfront_client():
    return boto3.client("cloudfront", config=_boto_config)


@lru_cache(maxsize=None)
def _get_cost_explorer_client():
    return boto3.client("ce", config=_boto_config)


@lru_cache(maxsize=None)
def _get_securityhub_client():
    return boto3.client("securityhub", config=_boto_config)


@lru_cache(maxsize=None)
def _get_config_client():
    return boto3.client("config", config=_boto_config)


@lru_cache(maxsize=None)
def _get_iam_client():
    return boto3.client("iam", config=_boto_config)


@lru_cache(maxsize=None)
def _get_autoscaling_client():
    return boto3.client("autoscaling", config=_boto_config)


async def get_cloudwatch_metrics(
    namespace: str,
    metric_name: str,
    dimensions: Optional[list[dict]] = None,
    period: int = 300,
    minutes: int = 60,
    statistics: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Fetch CloudWatch metric data points."""
    try:
        cw = _get_cloudwatch_client()
        loop = asyncio.get_event_loop()
        kwargs = {
            "Namespace": namespace,
            "MetricName": metric_name,
            "StartTime": datetime.utcnow() - timedelta(minutes=minutes),
            "EndTime": datetime.utcnow(),
            "Period": period,
            "Statistics": statistics or ["Average", "Maximum", "Minimum"],
        }
        if dimensions:
            kwargs["Dimensions"] = dimensions
        response = await loop.run_in_executor(
            None, lambda: cw.get_metric_statistics(**kwargs)
        )
        datapoints = sorted(response.get("Datapoints", []), key=lambda x: x["Timestamp"])
        return [
            {
                "timestamp": dp["Timestamp"].isoformat(),
                "avg": dp.get("Average"),
                "max": dp.get("Maximum"),
                "min": dp.get("Minimum"),
                "sum": dp.get("Sum"),
                "sample_count": dp.get("SampleCount"),
            }
            for dp in datapoints
        ]
    except (ClientError, BotoCoreError) as e:
        log.error("CloudWatch metric fetch error (%s/%s): %s", namespace, metric_name, e)
        return []


async def get_ec2_instances() -> list[dict[str, Any]]:
    """Fetch all EC2 instances with state and type."""
    try:
        ec2 = _get_ec2_client()
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, ec2.describe_instances)
        instances = []
        for res in response.get("Reservations", []):
            for inst in res.get("Instances", []):
                instances.append({
                    "id": inst["InstanceId"],
                    "type": inst.get("InstanceType", "unknown"),
                    "state": inst.get("State", {}).get("Name", "unknown"),
                    "launch_time": inst.get("LaunchTime").isoformat() if inst.get("LaunchTime") else None,
                    "private_ip": inst.get("PrivateIpAddress"),
                    "public_ip": inst.get("PublicIpAddress"),
                    "az": inst.get("Placement", {}).get("AvailabilityZone"),
                    "name": next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), ""),
                    "vpc_id": inst.get("VpcId"),
                    "subnet_id": inst.get("SubnetId"),
                    "security_groups": [sg["GroupId"] for sg in inst.get("SecurityGroups", [])],
                    "iam_profile": inst.get("IamInstanceProfile", {}).get("Arn"),
                    "monitoring": inst.get("Monitoring", {}).get("State", "disabled"),
                })
        return instances
    except (ClientError, BotoCoreError) as e:
        log.error("EC2 fetch error: %s", e)
        return []


async def get_ec2_detailed_metrics(instance_id: str) -> dict[str, Any]:
    """Get comprehensive EC2 metrics."""
    cpu, disk_read, disk_write, network_in, network_out, status_check, credit_balance = await asyncio.gather(
        get_cloudwatch_metrics("AWS/EC2", "CPUUtilization", [{"Name": "InstanceId", "Value": instance_id}]),
        get_cloudwatch_metrics("AWS/EC2", "DiskReadBytes", [{"Name": "InstanceId", "Value": instance_id}]),
        get_cloudwatch_metrics("AWS/EC2", "DiskWriteBytes", [{"Name": "InstanceId", "Value": instance_id}]),
        get_cloudwatch_metrics("AWS/EC2", "NetworkIn", [{"Name": "InstanceId", "Value": instance_id}]),
        get_cloudwatch_metrics("AWS/EC2", "NetworkOut", [{"Name": "InstanceId", "Value": instance_id}]),
        get_cloudwatch_metrics("AWS/EC2", "StatusCheckFailed", [{"Name": "InstanceId", "Value": instance_id}]),
        get_cloudwatch_metrics("AWS/EC2", "CPUCreditBalance", [{"Name": "InstanceId", "Value": instance_id}]),
    )
    return {
        "instance_id": instance_id,
        "cpu": cpu[-1] if cpu else None,
        "disk_read_bytes": disk_read[-1] if disk_read else None,
        "disk_write_bytes": disk_write[-1] if disk_write else None,
        "network_in": network_in[-1] if network_in else None,
        "network_out": network_out[-1] if network_out else None,
        "status_check_failed": status_check[-1] if status_check else None,
        "cpu_credit_balance": credit_balance[-1] if credit_balance else None,
    }


async def get_rds_instances() -> list[dict[str, Any]]:
    """Fetch all RDS instances with status and engine."""
    try:
        rds = _get_rds_client()
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, rds.describe_db_instances)
        instances = []
        for db in response.get("DBInstances", []):
            instances.append({
                "id": db["DBInstanceIdentifier"],
                "engine": db.get("Engine", "unknown"),
                "engine_version": db.get("EngineVersion", ""),
                "status": db.get("DBInstanceStatus", "unknown"),
                "class": db.get("DBInstanceClass", ""),
                "storage": db.get("AllocatedStorage", 0),
                "storage_type": db.get("StorageType", ""),
                "endpoint": db.get("Endpoint", {}).get("Address") if db.get("Endpoint") else None,
                "port": db.get("Endpoint", {}).get("Port") if db.get("Endpoint") else None,
                "az": db.get("AvailabilityZone"),
                "multi_az": db.get("MultiAZ", False),
                "backup_retention": db.get("BackupRetentionPeriod", 0),
                "encrypted": db.get("StorageEncrypted", False),
                "publicly_accessible": db.get("PubliclyAccessible", False),
                "vpc_security_groups": [sg["VpcSecurityGroupId"] for sg in db.get("VpcSecurityGroups", [])],
            })
        return instances
    except (ClientError, BotoCoreError) as e:
        log.error("RDS fetch error: %s", e)
        return []


# RDS has no "total memory" field on any Describe API — CloudWatch only exposes
# FreeableMemory (bytes). Convert to % used via each instance class's known RAM;
# classes not in this table degrade to None (honest "no data") rather than
# faking a percentage. Covers the burstable/general/memory-optimized families in
# use here — extend as new classes get provisioned.
_RDS_INSTANCE_MEMORY_GIB: dict[str, int] = {
    "db.t3.micro": 1, "db.t3.small": 2, "db.t3.medium": 4, "db.t3.large": 8,
    "db.t3.xlarge": 16, "db.t3.2xlarge": 32,
    "db.t4g.micro": 1, "db.t4g.small": 2, "db.t4g.medium": 4, "db.t4g.large": 8,
    "db.t4g.xlarge": 16, "db.t4g.2xlarge": 32,
    "db.m5.large": 8, "db.m5.xlarge": 16, "db.m5.2xlarge": 32, "db.m5.4xlarge": 64,
    "db.m6g.large": 8, "db.m6g.xlarge": 16, "db.m6g.2xlarge": 32, "db.m6g.4xlarge": 64,
    "db.r5.large": 16, "db.r5.xlarge": 32, "db.r5.2xlarge": 64, "db.r5.4xlarge": 128,
    "db.r6g.large": 16, "db.r6g.xlarge": 32, "db.r6g.2xlarge": 64, "db.r6g.4xlarge": 128,
}


def _rds_memory_pct_used(db_class: str, freeable_bytes: Optional[float]) -> Optional[float]:
    """% memory used from FreeableMemory bytes; None when the class or the
    CloudWatch datapoint is unavailable — never fabricate a number."""
    gib = _RDS_INSTANCE_MEMORY_GIB.get(db_class)
    if gib is None or freeable_bytes is None:
        return None
    pct = 100.0 * (1 - (freeable_bytes / (gib * 1024 ** 3)))
    return max(0.0, min(100.0, pct))


async def get_rds_detailed_metrics(db_instance_id: str) -> dict[str, Any]:
    """Get comprehensive RDS metrics."""
    cpu, connections, memory, storage, read_latency, write_latency, replica_lag, read_iops, write_iops = await asyncio.gather(
        get_cloudwatch_metrics("AWS/RDS", "CPUUtilization", [{"Name": "DBInstanceIdentifier", "Value": db_instance_id}]),
        get_cloudwatch_metrics("AWS/RDS", "DatabaseConnections", [{"Name": "DBInstanceIdentifier", "Value": db_instance_id}]),
        get_cloudwatch_metrics("AWS/RDS", "FreeableMemory", [{"Name": "DBInstanceIdentifier", "Value": db_instance_id}]),
        get_cloudwatch_metrics("AWS/RDS", "FreeStorageSpace", [{"Name": "DBInstanceIdentifier", "Value": db_instance_id}]),
        get_cloudwatch_metrics("AWS/RDS", "ReadLatency", [{"Name": "DBInstanceIdentifier", "Value": db_instance_id}]),
        get_cloudwatch_metrics("AWS/RDS", "WriteLatency", [{"Name": "DBInstanceIdentifier", "Value": db_instance_id}]),
        get_cloudwatch_metrics("AWS/RDS", "ReplicaLag", [{"Name": "DBInstanceIdentifier", "Value": db_instance_id}]),
        get_cloudwatch_metrics("AWS/RDS", "ReadIOPS", [{"Name": "DBInstanceIdentifier", "Value": db_instance_id}]),
        get_cloudwatch_metrics("AWS/RDS", "WriteIOPS", [{"Name": "DBInstanceIdentifier", "Value": db_instance_id}]),
    )
    return {
        "instance_id": db_instance_id,
        "cpu": cpu[-1] if cpu else None,
        "connections": connections[-1] if connections else None,
        "freeable_memory": memory[-1] if memory else None,
        "free_storage": storage[-1] if storage else None,
        "read_latency": read_latency[-1] if read_latency else None,
        "write_latency": write_latency[-1] if write_latency else None,
        "replica_lag": replica_lag[-1] if replica_lag else None,
        "read_iops": read_iops[-1] if read_iops else None,
        "write_iops": write_iops[-1] if write_iops else None,
    }


async def get_lambda_functions() -> list[dict[str, Any]]:
    """Fetch Lambda functions with runtime and timeout."""
    try:
        lambda_client = _get_lambda_client()
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda_client.list_functions)
        functions = []
        for fn in response.get("Functions", []):
            functions.append({
                "name": fn["FunctionName"],
                "runtime": fn.get("Runtime", "unknown"),
                "timeout": fn.get("Timeout", 0),
                "memory": fn.get("MemorySize", 0),
                "last_modified": fn.get("LastModified"),
                "handler": fn.get("Handler", ""),
                "code_size": fn.get("CodeSize", 0),
                "description": fn.get("Description", ""),
                "role": fn.get("Role", ""),
                "layers": fn.get("Layers", []),
                "state": fn.get("State", "Active"),
            })
        return functions
    except (ClientError, BotoCoreError) as e:
        log.error("Lambda fetch error: %s", e)
        return []


async def get_lambda_metrics(function_name: str) -> dict[str, Any]:
    """Get Lambda metrics."""
    invocations, errors, throttles, duration, concurrent = await asyncio.gather(
        get_cloudwatch_metrics("AWS/Lambda", "Invocations", [{"Name": "FunctionName", "Value": function_name}]),
        get_cloudwatch_metrics("AWS/Lambda", "Errors", [{"Name": "FunctionName", "Value": function_name}]),
        get_cloudwatch_metrics("AWS/Lambda", "Throttles", [{"Name": "FunctionName", "Value": function_name}]),
        get_cloudwatch_metrics("AWS/Lambda", "Duration", [{"Name": "FunctionName", "Value": function_name}]),
        get_cloudwatch_metrics("AWS/Lambda", "ConcurrentExecutions", [{"Name": "FunctionName", "Value": function_name}]),
    )
    return {
        "function_name": function_name,
        "invocations": invocations,
        "errors": errors[-1] if errors else None,
        "throttles": throttles[-1] if throttles else None,
        "duration_avg": duration[-1] if duration else None,
        "concurrent": concurrent[-1] if concurrent else None,
    }


async def get_ebs_volumes() -> list[dict[str, Any]]:
    """Fetch EBS volumes with state and size."""
    try:
        ec2 = _get_ec2_client()
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, ec2.describe_volumes)
        volumes = []
        for vol in response.get("Volumes", []):
            attachments = [
                {"instance_id": att["InstanceId"], "device": att["Device"], "state": att["State"]}
                for att in vol.get("Attachments", [])
            ]
            volumes.append({
                "id": vol["VolumeId"],
                "type": vol.get("VolumeType", "unknown"),
                "size_gb": vol.get("Size", 0),
                "state": vol.get("State", "unknown"),
                "iops": vol.get("Iops", 0),
                "throughput": vol.get("Throughput", 0),
                "attachments": attachments,
                "az": vol.get("AvailabilityZone"),
                "encrypted": vol.get("Encrypted", False),
                "snapshot_id": vol.get("SnapshotId", ""),
                "create_time": vol.get("CreateTime").isoformat() if vol.get("CreateTime") else None,
            })
        return volumes
    except (ClientError, BotoCoreError) as e:
        log.error("EBS fetch error: %s", e)
        return []


async def get_ebs_metrics(volume_id: str) -> dict[str, Any]:
    """Get EBS volume metrics."""
    read_bytes, write_bytes, read_ops, write_ops, queue_length, burst_balance = await asyncio.gather(
        get_cloudwatch_metrics("AWS/EBS", "VolumeReadBytes", [{"Name": "VolumeId", "Value": volume_id}]),
        get_cloudwatch_metrics("AWS/EBS", "VolumeWriteBytes", [{"Name": "VolumeId", "Value": volume_id}]),
        get_cloudwatch_metrics("AWS/EBS", "VolumeReadOps", [{"Name": "VolumeId", "Value": volume_id}]),
        get_cloudwatch_metrics("AWS/EBS", "VolumeWriteOps", [{"Name": "VolumeId", "Value": volume_id}]),
        get_cloudwatch_metrics("AWS/EBS", "VolumeQueueLength", [{"Name": "VolumeId", "Value": volume_id}]),
        get_cloudwatch_metrics("AWS/EBS", "BurstBalance", [{"Name": "VolumeId", "Value": volume_id}]),
    )
    return {
        "volume_id": volume_id,
        "read_bytes": read_bytes[-1] if read_bytes else None,
        "write_bytes": write_bytes[-1] if write_bytes else None,
        "read_ops": read_ops[-1] if read_ops else None,
        "write_ops": write_ops[-1] if write_ops else None,
        "queue_length": queue_length[-1] if queue_length else None,
        "burst_balance": burst_balance[-1] if burst_balance else None,
    }


async def get_load_balancers() -> list[dict[str, Any]]:
    """Fetch Application/Network Load Balancers."""
    try:
        elbv2 = _get_elbv2_client()
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, elbv2.describe_load_balancers)
        load_balancers = []
        for lb in response.get("LoadBalancers", []):
            load_balancers.append({
                "name": lb["LoadBalancerName"],
                "arn": lb["LoadBalancerArn"],
                "dns_name": lb.get("DNSName", ""),
                "type": lb.get("Type", "application"),
                "scheme": lb.get("Scheme", "internet-facing"),
                "state": lb.get("State", {}).get("Code", "unknown"),
                "vpc_id": lb.get("VpcId"),
                "azs": [az["ZoneName"] for az in lb.get("AvailabilityZones", [])],
                "created_time": lb.get("CreatedTime").isoformat() if lb.get("CreatedTime") else None,
            })
        return load_balancers
    except (ClientError, BotoCoreError) as e:
        log.error("ELB fetch error: %s", e)
        return []


async def get_load_balancer_metrics(lb_name: str) -> dict[str, Any]:
    """Get Load Balancer metrics."""
    request_count, target_response_time, healthy_hosts, unhealthy_hosts, http_5xx = await asyncio.gather(
        get_cloudwatch_metrics("AWS/ApplicationELB", "RequestCount", [{"Name": "LoadBalancer", "Value": lb_name}]),
        get_cloudwatch_metrics("AWS/ApplicationELB", "TargetResponseTime", [{"Name": "LoadBalancer", "Value": lb_name}]),
        get_cloudwatch_metrics("AWS/ApplicationELB", "HealthyHostCount", [{"Name": "LoadBalancer", "Value": lb_name}]),
        get_cloudwatch_metrics("AWS/ApplicationELB", "UnhealthyHostCount", [{"Name": "LoadBalancer", "Value": lb_name}]),
        get_cloudwatch_metrics("AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", [{"Name": "LoadBalancer", "Value": lb_name}]),
    )
    return {
        "load_balancer_name": lb_name,
        "request_count": request_count[-1] if request_count else None,
        "target_response_time": target_response_time[-1] if target_response_time else None,
        "healthy_hosts": healthy_hosts[-1] if healthy_hosts else None,
        "unhealthy_hosts": unhealthy_hosts[-1] if unhealthy_hosts else None,
        "http_5xx": http_5xx[-1] if http_5xx else None,
    }


async def get_s3_buckets() -> list[dict[str, Any]]:
    """Fetch all S3 buckets."""
    try:
        s3 = _get_s3_client()
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, s3.list_buckets)
        buckets = []
        for buck in response.get("Buckets", []):
            buckets.append({
                "name": buck["Name"],
                "created": buck["CreationDate"].isoformat() if buck.get("CreationDate") else None,
            })
        return buckets
    except (ClientError, BotoCoreError) as e:
        log.error("S3 fetch error: %s", e)
        return []


async def get_s3_metrics(bucket_name: str) -> dict[str, Any]:
    """Get S3 bucket metrics."""
    bucket_size_bytes, number_of_objects, get_requests, put_requests, bytes_downloaded, bytes_uploaded = await asyncio.gather(
        get_cloudwatch_metrics("AWS/S3", "BucketSizeBytes", [
            {"Name": "BucketName", "Value": bucket_name},
            {"Name": "StorageType", "Value": "StandardStorage"}
        ]),
        get_cloudwatch_metrics("AWS/S3", "NumberOfObjects", [
            {"Name": "BucketName", "Value": bucket_name},
            {"Name": "StorageType", "Value": "AllStorageTypes"}
        ]),
        get_cloudwatch_metrics("AWS/S3", "GetRequests", [{"Name": "BucketName", "Value": bucket_name}]),
        get_cloudwatch_metrics("AWS/S3", "PutRequests", [{"Name": "BucketName", "Value": bucket_name}]),
        get_cloudwatch_metrics("AWS/S3", "BytesDownloaded", [{"Name": "BucketName", "Value": bucket_name}]),
        get_cloudwatch_metrics("AWS/S3", "BytesUploaded", [{"Name": "BucketName", "Value": bucket_name}]),
    )
    return {
        "bucket_name": bucket_name,
        "size_bytes": bucket_size_bytes[-1] if bucket_size_bytes else None,
        "object_count": number_of_objects[-1] if number_of_objects else None,
        "get_requests": get_requests[-1] if get_requests else None,
        "put_requests": put_requests[-1] if put_requests else None,
        "bytes_downloaded": bytes_downloaded[-1] if bytes_downloaded else None,
        "bytes_uploaded": bytes_uploaded[-1] if bytes_uploaded else None,
    }


async def get_cloudfront_distributions() -> list[dict[str, Any]]:
    """Fetch CloudFront distributions."""
    try:
        cf = _get_cloudfront_client()
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, cf.list_distributions)
        distributions = []
        for dist in response.get("DistributionList", {}).get("Items", []):
            distributions.append({
                "id": dist["Id"],
                "domain_name": dist.get("DomainName", ""),
                "status": dist.get("Status", ""),
                "enabled": dist.get("Enabled", False),
                "comment": dist.get("Comment", ""),
                "price_class": dist.get("PriceClass", ""),
                "aliases": dist.get("Aliases", {}).get("Items", []),
                "origins": [o.get("DomainName", "") for o in dist.get("Origins", {}).get("Items", [])],
            })
        return distributions
    except (ClientError, BotoCoreError) as e:
        log.error("CloudFront fetch error: %s", e)
        return []


# Cost Explorer isn't enabled on every account (billing console access must be
# opted in). Once GetCostAndUsage proves that, short-circuit — otherwise the 15s
# broadcast loop spams paid GetCostAndUsage calls and ERROR logs forever (same
# failure mode fixed for Security Hub below).
_billing_disabled = False


async def get_billing_metrics() -> dict[str, Any]:
    """Fetch AWS cost and usage data (no-op if Cost Explorer isn't available)."""
    global _billing_disabled
    if _billing_disabled:
        return {"by_service": {}, "month_total": 0.0}
    try:
        ce = _get_cost_explorer_client()
        loop = asyncio.get_event_loop()
        end_date = datetime.utcnow().date()
        start_date = end_date - timedelta(days=30)
        response = await loop.run_in_executor(
            None,
            lambda: ce.get_cost_and_usage(
                TimePeriod={"Start": start_date.isoformat(), "End": end_date.isoformat()},
                Granularity="DAILY",
                Metrics=["UnblendedCost", "UsageQuantity"],
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            ),
        )
        daily_costs = []
        for day in response.get("ResultsByTime", []):
            date_str = day["TimePeriod"]["Start"]
            for group in day.get("Groups", []):
                daily_costs.append({
                    "date": date_str,
                    "service": group["Keys"][0],
                    "cost": float(group["Metrics"]["UnblendedCost"]["Amount"]),
                    "usage": float(group["Metrics"]["UsageQuantity"].get("Amount", 0)),
                })
        by_service = {}
        for item in daily_costs:
            service = item["service"]
            if service not in by_service:
                by_service[service] = {"total": 0.0, "daily": [], "usage_total": 0.0}
            by_service[service]["total"] += item["cost"]
            by_service[service]["usage_total"] += item["usage"]
            by_service[service]["daily"].append({"date": item["date"], "cost": item["cost"]})
        total_month = sum(s["total"] for s in by_service.values())
        return {
            "by_service": by_service,
            "month_total": round(total_month, 2),
            "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
            "daily": daily_costs,
        }
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        # Stable "Cost Explorer not enabled / no access" conditions: log ONCE and
        # stop calling GetCostAndUsage so we don't spam ERROR logs (and billed
        # API calls) every 15s.
        if code in ("DataUnavailableException", "AccessDeniedException", "SubscriptionRequiredException"):
            log.warning("Cost Explorer unavailable for this account (%s) — skipping billing.", code)
            _billing_disabled = True
            return {"by_service": {}, "month_total": 0.0}
        log.error("Billing fetch error: %s", e)
        return {"by_service": {}, "month_total": 0.0, "error": str(e)}
    except BotoCoreError as e:
        log.error("Billing fetch error: %s", e)
        return {"by_service": {}, "month_total": 0.0, "error": str(e)}


# Security Hub isn't enabled on every account. Once GetFindings proves the
# account isn't subscribed we short-circuit — otherwise the 15s broadcast loop
# spams ~240 ERROR logs/hour (and as many failing GetFindings calls) forever.
_securityhub_disabled = False


async def get_security_hub_findings() -> list[dict[str, Any]]:
    """Fetch Security Hub findings (no-op if the account isn't subscribed)."""
    global _securityhub_disabled
    if _securityhub_disabled:
        return []
    try:
        sh = _get_securityhub_client()
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: sh.get_findings(
                Filters={
                    "RecordState": [{"Value": "ACTIVE", "Comparison": "EQUALS"}],
                    "WorkflowStatus": [{"Value": "NEW", "Comparison": "EQUALS"}],
                },
                MaxResults=100
            )
        )
        findings = []
        for f in response.get("Findings", []):
            findings.append({
                "id": f.get("Id", ""),
                "title": f.get("Title", ""),
                "severity": f.get("Severity", {}).get("Label", "INFORMATIONAL"),
                "type": f.get("Types", [""])[0] if f.get("Types") else "",
                "product_name": f.get("ProductName", ""),
                "aws_account_id": f.get("AwsAccountId", ""),
                "created_at": f.get("CreatedAt"),
                "updated_at": f.get("UpdatedAt"),
                "compliance_status": f.get("Compliance", {}).get("Status", ""),
                "resources": [r.get("Id", "") for r in f.get("Resources", [])],
            })
        return findings
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        # Stable "Security Hub not turned on / no access" conditions: log ONCE and
        # stop calling GetFindings so we don't spam ERROR logs every 15s.
        if code in ("InvalidAccessException", "ResourceNotFoundException", "AccessDeniedException", "SubscriptionRequiredException"):
            log.warning("Security Hub unavailable for this account (%s) — skipping findings.", code)
            _securityhub_disabled = True
            return []
        log.error("Security Hub fetch error: %s", e)
        return []
    except BotoCoreError as e:
        log.error("Security Hub fetch error: %s", e)
        return []


async def get_iam_users() -> list[dict[str, Any]]:
    """Fetch IAM users."""
    try:
        iam = _get_iam_client()
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, iam.list_users)
        users = []
        for user in response.get("Users", []):
            users.append({
                "name": user["UserName"],
                "arn": user["Arn"],
                "user_id": user.get("UserId", ""),
                "create_date": user.get("CreateDate").isoformat() if user.get("CreateDate") else None,
                "password_last_used": user.get("PasswordLastUsed").isoformat() if user.get("PasswordLastUsed") else None,
                "path": user.get("Path", "/"),
            })
        return users
    except (ClientError, BotoCoreError) as e:
        log.error("IAM users fetch error: %s", e)
        return []


async def get_vpc_security_groups() -> list[dict[str, Any]]:
    """Fetch VPC security groups."""
    try:
        ec2 = _get_ec2_client()
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, ec2.describe_security_groups)
        security_groups = []
        for sg in response.get("SecurityGroups", []):
            ingress_rules = [
                {
                    "port": r.get("FromPort"),
                    "protocol": r.get("IpProtocol", ""),
                    "source": ",".join([ip.get("CidrIp", "") for ip in r.get("IpRanges", [])]),
                }
                for r in sg.get("IpPermissions", [])
            ]
            egress_rules = [
                {
                    "port": r.get("FromPort"),
                    "protocol": r.get("IpProtocol", ""),
                    "dest": ",".join([ip.get("CidrIp", "") for ip in r.get("IpRanges", [])]),
                }
                for r in sg.get("IpPermissionsEgress", [])
            ]
            security_groups.append({
                "id": sg["GroupId"],
                "name": sg["GroupName"],
                "vpc_id": sg.get("VpcId", ""),
                "description": sg.get("Description", ""),
                "ingress_rules": ingress_rules,
                "egress_rules": egress_rules,
            })
        return security_groups
    except (ClientError, BotoCoreError) as e:
        log.error("Security groups fetch error: %s", e)
        return []


async def get_auto_scaling_groups() -> list[dict[str, Any]]:
    """Fetch Auto Scaling Groups."""
    try:
        asg = _get_autoscaling_client()
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, asg.describe_auto_scaling_groups)
        groups = []
        for group in response.get("AutoScalingGroups", []):
            groups.append({
                "name": group["AutoScalingGroupName"],
                "min": group.get("MinSize", 0),
                "max": group.get("MaxSize", 0),
                "desired": group.get("DesiredCapacity", 0),
                "instances": [{"id": i["InstanceId"], "health": i.get("HealthStatus", "Unknown")} for i in group.get("Instances", [])],
                "launch_template": group.get("LaunchTemplate", {}).get("LaunchTemplateId", ""),
                "health_check_type": group.get("HealthCheckType", "EC2"),
                "vpc_zone_identifier": group.get("VPCZoneIdentifier", ""),
            })
        return groups
    except (ClientError, BotoCoreError) as e:
        log.error("ASG fetch error: %s", e)
        return []


async def get_full_infrastructure_snapshot() -> dict[str, Any]:
    """Get complete AWS infrastructure overview."""
    ec2, rds, s3, lambdas, ebs, elb, cloudfront, billing, security_findings, iam_users, security_groups, asg = await asyncio.gather(
        get_ec2_instances(),
        get_rds_instances(),
        get_s3_buckets(),
        get_lambda_functions(),
        get_ebs_volumes(),
        get_load_balancers(),
        get_cloudfront_distributions(),
        get_billing_metrics(),
        get_security_hub_findings(),
        get_iam_users(),
        get_vpc_security_groups(),
        get_auto_scaling_groups(),
    )
    
    security_score = 100 - (len([f for f in security_findings if f.get("severity") in ["CRITICAL", "HIGH"]]) * 10)
    security_score = max(0, min(100, security_score))
    
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "ec2": {"instances": ec2, "count": len(ec2)},
        "rds": {"instances": rds, "count": len(rds)},
        "s3": {"buckets": s3, "count": len(s3)},
        "lambda": {"functions": lambdas, "count": len(lambdas)},
        "ebs": {"volumes": ebs, "count": len(ebs)},
        "elb": {"load_balancers": elb, "count": len(elb)},
        "cloudfront": {"distributions": cloudfront, "count": len(cloudfront)},
        "billing": billing,
        "security": {
            "findings": security_findings,
            "findings_count": len(security_findings),
            "score": security_score,
            "iam_users": iam_users,
            "security_groups": security_groups,
        },
        "auto_scaling": {"groups": asg, "count": len(asg)},
    }


@router.get("/infrastructure")
async def api_get_infrastructure(
    ctx: TenantContext = Depends(require_context),
) -> JSONResponse:
    """Get full AWS infrastructure snapshot."""
    require_role(ctx, Role.PLATFORM_ADMIN, Role.BROKER_OWNER)
    snapshot = await get_full_infrastructure_snapshot()
    return JSONResponse(snapshot)


@router.get("/ec2/instances")
async def api_get_ec2(
    ctx: TenantContext = Depends(require_context),
) -> JSONResponse:
    """Get EC2 instances."""
    require_role(ctx, Role.PLATFORM_ADMIN, Role.BROKER_OWNER)
    instances = await get_ec2_instances()
    return JSONResponse({"instances": instances})


@router.get("/ec2/{instance_id}/metrics")
async def api_get_ec2_metrics(
    instance_id: str,
    ctx: TenantContext = Depends(require_context),
) -> JSONResponse:
    """Get EC2 detailed metrics."""
    require_role(ctx, Role.PLATFORM_ADMIN, Role.BROKER_OWNER)
    metrics = await get_ec2_detailed_metrics(instance_id)
    return JSONResponse(metrics)


# Range -> (lookback minutes, CloudWatch period seconds). ~60-170 points each.
_HISTORY_RANGES = {
    "1h": (60, 60),
    "6h": (360, 300),
    "24h": (1440, 900),
    "7d": (10080, 3600),
    "30d": (43200, 21600),
}
# resource type -> (namespace, metric, dimension name, unit, statistic)
_HISTORY_METRICS = {
    "ec2": ("AWS/EC2", "CPUUtilization", "InstanceId", "Percent", "Average"),
    "rds": ("AWS/RDS", "CPUUtilization", "DBInstanceIdentifier", "Percent", "Average"),
    "lambda": ("AWS/Lambda", "Invocations", "FunctionName", "Count", "Sum"),
}


@router.get("/metrics/history")
async def api_metrics_history(
    type: str,
    id: str,
    range: str = "1h",
    ctx: TenantContext = Depends(require_context),
) -> JSONResponse:
    """Real historical CloudWatch series for one resource — feeds the dashboard
    time-series charts (replaces the old client-side fake sparkline). CloudWatch
    IS the time-series store (15-month retention); nothing is persisted here."""
    require_role(ctx, Role.PLATFORM_ADMIN, Role.BROKER_OWNER)
    if type not in _HISTORY_METRICS:
        return JSONResponse({"error": f"unknown type '{type}'"}, status_code=400)
    minutes, period = _HISTORY_RANGES.get(range, _HISTORY_RANGES["1h"])
    namespace, metric, dim_name, unit, stat = _HISTORY_METRICS[type]
    points = await get_cloudwatch_metrics(
        namespace, metric, [{"Name": dim_name, "Value": id}],
        period=period, minutes=minutes, statistics=[stat, "Maximum"],
    )
    val_key = "sum" if stat == "Sum" else "avg"
    series = [{"t": p["timestamp"], "v": p.get(val_key), "max": p.get("max")} for p in points]
    return JSONResponse({
        "type": type, "id": id, "metric": metric, "unit": unit,
        "range": range, "period": period, "series": series,
    })


def _build_copilot_prompt(message: str, context: dict) -> str:
    ctx = json.dumps(context, default=str)[:3000]
    return (
        "You are NAVI, an expert AWS cloud operations copilot embedded in a live "
        "infrastructure dashboard. Answer the operator concisely (2-5 sentences). "
        "Cite numbers ONLY from the live context below; never invent resources. If "
        "asked to act, give the specific AWS action or CLI command.\n\n"
        f"LIVE CONTEXT (JSON): {ctx}\n\n"
        f"OPERATOR: {message}\n\nNAVI:"
    )


@router.post("/copilot/query")
async def api_copilot_query(
    body: CopilotRequest,
    ctx: TenantContext = Depends(require_context),
) -> JSONResponse:
    """Real LLM copilot (Bedrock) answering ops questions with the live infra
    snapshot as context. Replaces the client-side if/else responder; the frontend
    falls back to its rule-based answers if this is unavailable."""
    require_role(ctx, Role.PLATFORM_ADMIN, Role.BROKER_OWNER)
    _check_copilot_rate_limit(ctx)
    message = body.message.strip()
    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)
    encoded_context = json.dumps(body.context, separators=(",", ":"), default=str).encode("utf-8")
    if len(encoded_context) > _COPILOT_CONTEXT_MAX_BYTES:
        return JSONResponse({"error": "context too large"}, status_code=413)
    prompt = _build_copilot_prompt(message, body.context)
    reply = None
    try:
        from ml_forge.bedrock_client import invoke_bedrock_model, PRIMARY_MODEL, SECONDARY_MODEL
        reply = await asyncio.to_thread(invoke_bedrock_model, PRIMARY_MODEL, prompt, 700)
        if not reply:
            reply = await asyncio.to_thread(invoke_bedrock_model, SECONDARY_MODEL, prompt, 700)
    except Exception as e:  # noqa: BLE001 — LLM/creds failures degrade to the client fallback
        log.error("Copilot LLM error: %s", e)
    if not reply:
        return JSONResponse({"reply": None, "error": "llm_unavailable"}, status_code=503)
    return JSONResponse({"reply": reply.strip(), "model": "bedrock"})


@router.get("/rds/instances")
async def api_get_rds(
    ctx: TenantContext = Depends(require_context),
) -> JSONResponse:
    """Get RDS instances."""
    require_role(ctx, Role.PLATFORM_ADMIN, Role.BROKER_OWNER)
    instances = await get_rds_instances()
    return JSONResponse({"instances": instances})


@router.get("/rds/{instance_id}/metrics")
async def api_get_rds_metrics(
    instance_id: str,
    ctx: TenantContext = Depends(require_context),
) -> JSONResponse:
    """Get RDS detailed metrics."""
    require_role(ctx, Role.PLATFORM_ADMIN, Role.BROKER_OWNER)
    metrics = await get_rds_detailed_metrics(instance_id)
    return JSONResponse(metrics)


@router.get("/lambda/functions")
async def api_get_lambda(ctx: TenantContext = Depends(require_context)) -> JSONResponse:
    """Get Lambda functions."""
    require_role(ctx, Role.PLATFORM_ADMIN, Role.BROKER_OWNER)
    functions = await get_lambda_functions()
    return JSONResponse({"functions": functions})


@router.get("/lambda/{function_name}/metrics")
async def api_get_lambda_metrics(
    function_name: str,
    ctx: TenantContext = Depends(require_context),
) -> JSONResponse:
    """Get Lambda metrics."""
    require_role(ctx, Role.PLATFORM_ADMIN, Role.BROKER_OWNER)
    metrics = await get_lambda_metrics(function_name)
    return JSONResponse(metrics)


@router.get("/ebs/volumes")
async def api_get_ebs(ctx: TenantContext = Depends(require_context)) -> JSONResponse:
    """Get EBS volumes."""
    require_role(ctx, Role.PLATFORM_ADMIN, Role.BROKER_OWNER)
    volumes = await get_ebs_volumes()
    return JSONResponse({"volumes": volumes})


@router.get("/ebs/{volume_id}/metrics")
async def api_get_ebs_metrics(
    volume_id: str,
    ctx: TenantContext = Depends(require_context),
) -> JSONResponse:
    """Get EBS metrics."""
    require_role(ctx, Role.PLATFORM_ADMIN, Role.BROKER_OWNER)
    metrics = await get_ebs_metrics(volume_id)
    return JSONResponse(metrics)


@router.get("/elb")
async def api_get_elb(ctx: TenantContext = Depends(require_context)) -> JSONResponse:
    """Get Load Balancers."""
    require_role(ctx, Role.PLATFORM_ADMIN, Role.BROKER_OWNER)
    load_balancers = await get_load_balancers()
    return JSONResponse({"load_balancers": load_balancers})


@router.get("/s3/buckets")
async def api_get_s3(ctx: TenantContext = Depends(require_context)) -> JSONResponse:
    """Get S3 buckets."""
    require_role(ctx, Role.PLATFORM_ADMIN, Role.BROKER_OWNER)
    buckets = await get_s3_buckets()
    return JSONResponse({"buckets": buckets})


@router.get("/s3/{bucket_name}/metrics")
async def api_get_s3_metrics(
    bucket_name: str,
    ctx: TenantContext = Depends(require_context),
) -> JSONResponse:
    """Get S3 metrics."""
    require_role(ctx, Role.PLATFORM_ADMIN, Role.BROKER_OWNER)
    metrics = await get_s3_metrics(bucket_name)
    return JSONResponse(metrics)


@router.get("/billing")
async def api_get_billing(
    ctx: TenantContext = Depends(require_context),
) -> JSONResponse:
    """Get AWS billing metrics."""
    require_role(ctx, Role.PLATFORM_ADMIN, Role.BROKER_OWNER)
    billing = await get_billing_metrics()
    return JSONResponse(billing)


@router.get("/security/findings")
async def api_get_security_findings(
    ctx: TenantContext = Depends(require_context),
) -> JSONResponse:
    """Get Security Hub findings."""
    require_role(ctx, Role.PLATFORM_ADMIN, Role.BROKER_OWNER)
    findings = await get_security_hub_findings()
    iam_users = await get_iam_users()
    security_groups = await get_vpc_security_groups()
    security_score = 100 - (len([f for f in findings if f.get("severity") in ["CRITICAL", "HIGH"]]) * 10)
    return JSONResponse({
        "findings": findings,
        "iam_users": iam_users,
        "security_groups": security_groups,
        "score": max(0, min(100, security_score)),
    })


@router.get("/security/groups")
async def api_get_security_groups(
    ctx: TenantContext = Depends(require_context),
) -> JSONResponse:
    """Get VPC Security Groups."""
    require_role(ctx, Role.PLATFORM_ADMIN, Role.BROKER_OWNER)
    groups = await get_vpc_security_groups()
    return JSONResponse({"security_groups": groups})


@router.get("/autoscaling/groups")
async def api_get_asg(ctx: TenantContext = Depends(require_context)) -> JSONResponse:
    """Get Auto Scaling Groups."""
    require_role(ctx, Role.PLATFORM_ADMIN, Role.BROKER_OWNER)
    groups = await get_auto_scaling_groups()
    return JSONResponse({"auto_scaling_groups": groups})


@router.post("/autoscaling/{group_name}/scale")
async def api_scale_asg(
    group_name: str,
    request: ScaleRequest,
    ctx: TenantContext = Depends(require_context),
) -> JSONResponse:
    """Scale an Auto Scaling Group."""
    require_role(ctx, Role.PLATFORM_ADMIN)
    if not _ASG_NAME_RE.fullmatch(group_name):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid Auto Scaling group name.")

    try:
        asg = _get_autoscaling_client()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: asg.set_desired_capacity(
                AutoScalingGroupName=group_name,
                DesiredCapacity=request.desired_capacity,
                HonorCooldown=True,
            )
        )
        log.info(
            "ASG desired capacity changed: group=%s desired=%d actor=%s",
            group_name,
            request.desired_capacity,
            ctx.agent_id,
        )
        return JSONResponse({"success": True, "group": group_name, "desired_capacity": request.desired_capacity})
    except (ClientError, BotoCoreError) as e:
        log.error("ASG scale error: %s", e)
        return JSONResponse({"error": "AWS scaling operation failed"}, status_code=502)


@router.post("/ec2/{instance_id}/restart")
async def api_restart_ec2(
    instance_id: str,
    ctx: TenantContext = Depends(require_context),
) -> JSONResponse:
    """Restart an EC2 instance."""
    require_role(ctx, Role.PLATFORM_ADMIN)
    if not _EC2_INSTANCE_RE.fullmatch(instance_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid EC2 instance ID.")

    try:
        ec2 = _get_ec2_client()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: ec2.reboot_instances(InstanceIds=[instance_id])
        )
        log.info("EC2 reboot requested: instance=%s actor=%s", instance_id, ctx.agent_id)
        return JSONResponse({"success": True, "instance_id": instance_id, "action": "reboot"})
    except (ClientError, BotoCoreError) as e:
        log.error("EC2 reboot error: %s", e)
        return JSONResponse({"error": "AWS reboot operation failed"}, status_code=502)


_obs_websockets: set[WebSocket] = set()


async def broadcast_metrics():
    """Periodically broadcast AWS metrics to all connected WebSocket clients.

    Skips the entire snapshot + CloudWatch/Cost-Explorer fan-out when no client
    is connected — otherwise the loop bills Cost Explorer (~$0.01 per
    GetCostAndUsage) every 15s around the clock with nobody watching.
    """
    while True:
        try:
            # Client gate: no viewers → no AWS API calls (and no billing).
            if not _obs_websockets:
                await asyncio.sleep(15)
                continue

            snapshot = await get_full_infrastructure_snapshot()

            ec2_list = snapshot["ec2"]["instances"][:10]
            rds_list = snapshot["rds"]["instances"][:5]
            lambda_list = snapshot["lambda"]["functions"][:5]

            # Fetch every per-resource CloudWatch series concurrently — serial
            # awaits here can exceed the 15s interval on a multi-instance fleet.
            ec2_cpu, rds_cpu, rds_memory, lambda_invocations = await asyncio.gather(
                asyncio.gather(*[
                    get_cloudwatch_metrics(
                        "AWS/EC2", "CPUUtilization",
                        [{"Name": "InstanceId", "Value": inst["id"]}])
                    for inst in ec2_list
                ]),
                asyncio.gather(*[
                    get_cloudwatch_metrics(
                        "AWS/RDS", "CPUUtilization",
                        [{"Name": "DBInstanceIdentifier", "Value": db["id"]}])
                    for db in rds_list
                ]),
                asyncio.gather(*[
                    get_cloudwatch_metrics(
                        "AWS/RDS", "FreeableMemory",
                        [{"Name": "DBInstanceIdentifier", "Value": db["id"]}])
                    for db in rds_list
                ]),
                asyncio.gather(*[
                    get_cloudwatch_metrics(
                        "AWS/Lambda", "Invocations",
                        [{"Name": "FunctionName", "Value": fn["name"]}])
                    for fn in lambda_list
                ]),
            )

            ec2_metrics = {
                inst["id"]: {
                    "cpu": cpu[-1] if cpu else None,
                    "name": inst["name"],
                    "state": inst["state"],
                    "type": inst["type"],
                }
                for inst, cpu in zip(ec2_list, ec2_cpu)
            }
            rds_metrics = {
                db["id"]: {
                    "cpu": cpu[-1] if cpu else None,
                    "memory": {
                        "avg": _rds_memory_pct_used(db.get("class", ""), mem[-1]["avg"])
                    } if mem and mem[-1].get("avg") is not None else None,
                    "name": db["id"],
                    "status": db["status"],
                    "engine": db["engine"],
                }
                for db, cpu, mem in zip(rds_list, rds_cpu, rds_memory)
            }
            lambda_metrics = {
                fn["name"]: {
                    "invocations": inv[-1] if inv else None,
                    "memory": fn["memory"],
                    "runtime": fn["runtime"],
                }
                for fn, inv in zip(lambda_list, lambda_invocations)
            }
            elb_metrics = {
                lb["name"]: {"state": lb["state"], "type": lb["type"]}
                for lb in snapshot["elb"]["load_balancers"][:3]
            }

            payload = {
                "type": "AWS_METRICS_UPDATE",
                "timestamp": snapshot["timestamp"],
                "ec2": ec2_metrics,
                "rds": rds_metrics,
                "lambda": lambda_metrics,
                "elb": elb_metrics,
                "billing": snapshot["billing"],
                "security": {"score": snapshot["security"]["score"], "findings_count": snapshot["security"]["findings_count"]},
            }
            message = json.dumps(payload)
            # Iterate a snapshot copy — the WS handler mutates _obs_websockets on
            # connect/disconnect and send_text() yields the loop mid-iteration.
            for ws in list(_obs_websockets):
                try:
                    await ws.send_text(message)
                except Exception:
                    _obs_websockets.discard(ws)
        except Exception as e:
            log.error("Metrics broadcast error: %s", e)
        await asyncio.sleep(15)


@router.websocket("/ws")
async def observability_websocket(websocket: WebSocket):
    """WebSocket endpoint for real-time AWS observability stream.

    Auth-gated to match the REST routes. The browser sends two WebSocket
    subprotocol values: `oracle.jwt` and the JWT. This keeps credentials out of
    request URLs, browser history, and ordinary access logs.
    """
    offered_protocols = [
        value.strip()
        for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if value.strip()
    ]
    raw_token = offered_protocols[1] if len(offered_protocols) == 2 and offered_protocols[0] == "oracle.jwt" else ""
    try:
        claims = decode_token(raw_token)
        _validate_tenant_id(claims.get("tenant_id", ""))
    except Exception:  # noqa: BLE001 — missing/invalid/expired token
        await websocket.close(code=4401)  # 4401: application "unauthorized"
        log.warning("Observability WS rejected — missing/invalid token.")
        return
    if claims.get("role") not in _OBS_ALLOWED_ROLES:
        await websocket.close(code=4403)  # 4403: application "forbidden"
        log.warning("Observability WS rejected — role %r not permitted.", claims.get("role"))
        return

    await websocket.accept(subprotocol="oracle.jwt")
    _obs_websockets.add(websocket)
    log.info("Observability WebSocket connected — %d clients", len(_obs_websockets))
    last_snapshot_at = float("-inf")
    last_metrics_at = float("-inf")
    try:
        await websocket.send_text(json.dumps({"type": "AWS_CONNECTED"}))
        while True:
            try:
                data = await websocket.receive_text()
                if len(data.encode("utf-8")) > _WS_MAX_MESSAGE_BYTES:
                    await websocket.close(code=1009)
                    break
                msg = json.loads(data)
                if not isinstance(msg, dict):
                    await websocket.send_text(json.dumps({"type": "AWS_REQUEST_ERROR"}))
                    continue
                if msg.get("type") == "REQUEST_SNAPSHOT":
                    now = time.monotonic()
                    if now - last_snapshot_at < _WS_SNAPSHOT_COOLDOWN:
                        await websocket.send_text(json.dumps({"type": "AWS_RATE_LIMITED"}))
                        continue
                    last_snapshot_at = now
                    snapshot = await get_full_infrastructure_snapshot()
                    await websocket.send_text(json.dumps({
                        "type": "AWS_INFRASTRUCTURE_SNAPSHOT",
                        **snapshot,
                    }))
                elif msg.get("type") == "REQUEST_METRICS":
                    now = time.monotonic()
                    if now - last_metrics_at < _WS_METRICS_COOLDOWN:
                        await websocket.send_text(json.dumps({"type": "AWS_RATE_LIMITED"}))
                        continue
                    instance_id = msg.get("instance_id")
                    instance_type = msg.get("instance_type", "ec2")
                    pattern = _WS_RESOURCE_ID_PATTERNS.get(instance_type)
                    if (
                        pattern is None
                        or not isinstance(instance_id, str)
                        or pattern.fullmatch(instance_id) is None
                    ):
                        await websocket.send_text(json.dumps({"type": "AWS_REQUEST_ERROR"}))
                        continue
                    last_metrics_at = now
                    if instance_type == "ec2":
                        metrics = await get_ec2_detailed_metrics(instance_id)
                    elif instance_type == "rds":
                        metrics = await get_rds_detailed_metrics(instance_id)
                    elif instance_type == "lambda":
                        metrics = await get_lambda_metrics(instance_id)
                    elif instance_type == "ebs":
                        metrics = await get_ebs_metrics(instance_id)
                    await websocket.send_text(json.dumps({"type": "AWS_DETAILED_METRICS", **metrics}))
                elif msg.get("type") == "PING":
                    # Client heartbeat — reply so it can detect a dead connection.
                    await websocket.send_text(json.dumps({"type": "PONG"}))
            except WebSocketDisconnect:
                break
            except Exception as e:
                log.warning("WebSocket recv error: %s", e)
                break
    finally:
        _obs_websockets.discard(websocket)
        log.info("Observability WebSocket disconnected — %d clients", len(_obs_websockets))
