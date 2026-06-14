"""
Cached boto3 S3 client for media operations.
Reusing a single client avoids per-request connection overhead and reduces
confirm-upload and presigned-URL latency (especially when backend is far from AWS region).
"""
import boto3
from django.conf import settings

_s3_client = None


def get_s3_client():
    """Return a cached boto3 S3 client. Thread-safe; safe to call from multiple threads."""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client(
            's3',
            aws_access_key_id=getattr(settings, 'AWS_ACCESS_KEY_ID', ''),
            aws_secret_access_key=getattr(settings, 'AWS_SECRET_ACCESS_KEY', ''),
            region_name=getattr(settings, 'AWS_S3_REGION_NAME', 'us-east-1'),
        )
    return _s3_client
