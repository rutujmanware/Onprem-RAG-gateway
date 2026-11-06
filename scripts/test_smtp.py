#!/usr/bin/env python3
"""SMTP Configuration Diagnostic & Test Tool.

Usage:
  python scripts/test_smtp.py [recipient@iisc.ac.in]

Validates settings in smtp.env:
  1. Checks for non-empty SMTP_HOST
  2. Probes socket connectivity
  3. Tests SSL/STARTTLS encryption handshake
  4. Tests authentication with SMTP_USER / SMTP_PASS
  5. Optionally sends a sample test verification email if recipient is provided
"""

import os
import sys
import time
import socket
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

# Load environment from smtp.env
ROOT_DIR = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    for env_file in [ROOT_DIR / "smtp.env", ROOT_DIR / "askiisc_chat" / "smtp.env", ROOT_DIR / ".env"]:
        if env_file.exists():
            load_dotenv(str(env_file), override=False)
except ImportError:
    pass

SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_STARTTLS = os.getenv("SMTP_STARTTLS", "1") == "1"
SMTP_SSL = os.getenv("SMTP_SSL", "0") == "1" or SMTP_PORT == 465
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "").strip()
SMTP_FROM = os.getenv("SMTP_FROM", "askiisc-noreply@iisc.ac.in").strip()
SMTP_TIMEOUT = float(os.getenv("SMTP_TIMEOUT", "15"))

def main():
    print("=" * 65)
    print("📧 AskIISc SMTP Relay Diagnostic Utility")
    print("=" * 65)
    print(f"Target Configuration:")
    print(f"  • SMTP_HOST     : {SMTP_HOST or '(NOT SET - please fill in smtp.env)'}")
    print(f"  • SMTP_PORT     : {SMTP_PORT}")
    print(f"  • Encryption    : {'Direct SSL/TLS' if SMTP_SSL else ('STARTTLS' if SMTP_STARTTLS else 'None (Plaintext)')}")
    print(f"  • SMTP_USER     : {SMTP_USER or '(None - unauthenticated relay)'}")
    print(f"  • SMTP_PASS     : {'******' if SMTP_PASS else '(None)'}")
    print(f"  • SMTP_FROM     : {SMTP_FROM}")
    print("=" * 65)

    if not SMTP_HOST:
        print("\n❌ ERROR: SMTP_HOST is currently empty!")
        print("Please edit the 'smtp.env' file and fill in your SMTP server details:")
        print(f"  File location: {ROOT_DIR / 'smtp.env'}")
        print("\nExample configuration for Office365 / IISc Exchange:")
        print("  SMTP_HOST=smtp.office365.com")
        print("  SMTP_PORT=587")
        print("  SMTP_STARTTLS=1")
        print("  SMTP_USER=your_username@iisc.ac.in")
        print("  SMTP_PASS=your_app_password")
        print("  SMTP_FROM=your_username@iisc.ac.in")
        sys.exit(1)

    # Step 1: TCP Socket Reachability
    print(f"\n[1/4] Probing TCP connection to {SMTP_HOST}:{SMTP_PORT}...")
    t0 = time.time()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(SMTP_TIMEOUT)
        sock.connect((SMTP_HOST, SMTP_PORT))
        sock.close()
        print(f"      ✅ TCP port {SMTP_PORT} is open and reachable ({round((time.time() - t0) * 1000, 1)}ms)")
    except Exception as e:
        print(f"      ❌ FAILED to connect to {SMTP_HOST}:{SMTP_PORT}: {e}")
        print("      Check your network gate, firewall, or ensure the server is on the allowed intranet.")
        sys.exit(1)

    # Step 2 & 3: Handshake and Authentication
    print(f"\n[2/4] Initializing SMTP protocol and encryption handshake...")
    context = ssl.create_default_context()
    smtp_client = None
    try:
        if SMTP_SSL:
            print("      Connecting with direct SSL/TLS...")
            smtp_client = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT, context=context)
        else:
            print("      Connecting via standard SMTP...")
            smtp_client = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT)
            if SMTP_STARTTLS:
                print("      Upgrading connection to secure STARTTLS...")
                smtp_client.starttls(context=context)

        print("      ✅ Encryption handshake succeeded!")

        print(f"\n[3/4] Authenticating with SMTP server...")
        if SMTP_USER and SMTP_PASS:
            smtp_client.login(SMTP_USER, SMTP_PASS)
            print(f"      ✅ Successfully authenticated as '{SMTP_USER}'!")
        else:
            print("      ℹ️  No username/password provided (assuming open intranet relay).")

        # Step 4: Optional test email delivery
        recipient = sys.argv[1] if len(sys.argv) > 1 else None
        if recipient:
            print(f"\n[4/4] Sending test verification email to: {recipient}...")
            test_code = "123456"
            msg = EmailMessage()
            msg["From"] = SMTP_FROM
            msg["To"] = recipient
            msg["Subject"] = "AskIISc - SMTP Configuration Verification"
            msg.set_content(
                f"Hello,\n\n"
                f"This is a confirmation that your SMTP relay configuration is working properly!\n\n"
                f"Your AskIISc verification code test is: {test_code}\n\n"
                f"Configuration:\n"
                f"  Host: {SMTP_HOST}:{SMTP_PORT}\n"
                f"  Sender: {SMTP_FROM}\n\n"
                f"-- \nAskIISc On-Premise Thesis RAG Gateway"
            )
            smtp_client.send_message(msg)
            print(f"      🎉 Test email successfully sent to {recipient}!")
            print("      Please check your inbox (and spam folder) to confirm receipt.")
        else:
            print(f"\n[4/4] Email delivery test skipped (no recipient address passed).")
            print("      To send a test email, run:")
            print(f"        python scripts/test_smtp.py your_email@iisc.ac.in")

        smtp_client.quit()
        print("\n" + "=" * 65)
        print("🎉 ALL SMTP HEALTH CHECKS PASSED SUCCESSFULLY!")
        print("=" * 65)

    except smtplib.SMTPAuthenticationError as e:
        print(f"\n❌ AUTHENTICATION FAILED: {e}")
        print("Please double check your SMTP_USER and SMTP_PASS in smtp.env.")
        print("Note: If using Microsoft 365 / Google with 2FA, an App Password is required.")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ SMTP ERROR: {type(e).__name__}: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()

