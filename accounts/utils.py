import string
from django.conf import settings
import uuid
import random
from django.contrib.auth.hashers import make_password, check_password

def generate_random_password(length=10):
    chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@#$%"
    return "".join(random.choice(chars) for _ in range(length))

def generate_uuid():
    return str(uuid.uuid4())


def generate_random_username(prefix="user"):
    """Generate a unique random username for new users (backend-only)."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def generate_otp():
    return f"{random.randint(100000, 999999)}"

def hash_otp(otp: str) -> str:
    return make_password(otp)

def verify_otp(plain_otp, hashed_otp) -> bool:
    return check_password(plain_otp, hashed_otp)