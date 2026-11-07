#!/usr/bin/env python3
"""Utility to clear OTP rate limits and active OTP codes in Redis.

Usage:
  # Clear limits for a specific user:
  python scripts/clear_otp_limits.py ritujmanware@iisc.ac.in

  # Clear ALL OTP limits and cooldowns across the platform:
  python scripts/clear_otp_limits.py --all
"""
import sys
import os
from pathlib import Path

# Load environment
ROOT_DIR = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT_DIR / ".env", override=False)
except ImportError:
    pass

import redis

_redis_pw = os.getenv("REDIS_PASSWORD", "")
_pw_str = f":{_redis_pw}@" if _redis_pw else ""
_default_redis_url = f"redis://{_pw_str}{os.getenv('REDIS_HOST', 'localhost')}:{os.getenv('REDIS_PORT', '6379')}/0"
REDIS_URL = os.getenv("REDIS_URL") or _default_redis_url

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(0)

    arg = sys.argv[1].strip()
    r = redis.from_url(REDIS_URL)

    if arg in ("--all", "-a"):
        # Scan and delete all auth:rate:* and auth:otp:* keys
        deleted = 0
        for pattern in ["auth:rate:*", "auth:otp:*"]:
            keys = list(r.scan_iter(pattern))
            if keys:
                deleted += r.delete(*keys)
        print(f"✅ Cleared {deleted} OTP rate limit and authentication keys from Redis.")
    else:
        email = arg.lower()
        keys_to_delete = [
            f"auth:rate:email:{email}",
            f"auth:rate:verify:email:{email}",
            f"auth:otp:{email}",
        ]
        deleted = r.delete(*keys_to_delete)
        print(f"✅ Cleared OTP rate limits and active codes for '{email}' (keys removed: {deleted}).")

if __name__ == "__main__":
    main()

