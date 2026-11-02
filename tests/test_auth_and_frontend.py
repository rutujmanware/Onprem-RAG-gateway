import asyncio
import os
import sys
import time
from pathlib import Path

# Add gateway root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.auth.email_service import EmailService, email_service
from gateway.auth.guard import UserIdentity, require_iisc_user
from gateway.auth.token_service import token_service
from gateway.auth.user_store import UserStore, hash_password, verify_password


def test_token_service_domain_enforcement():
    # Allowed domains (@iisc.ac.in and @gmail.com)
    assert token_service.is_allowed_email("student@iisc.ac.in") is True
    assert token_service.is_allowed_email("faculty@ece.iisc.ac.in") is True
    assert token_service.is_allowed_email("admin@serc.iisc.ac.in") is True
    assert token_service.is_allowed_email("researcher@csa.iisc.ac.in") is True
    assert token_service.is_allowed_email("student@gmail.com") is True
    assert token_service.is_allowed_email("vivek12012031@gmail.com") is True

    # Blocked external domains
    assert token_service.is_allowed_email("user@yahoo.com") is False
    assert token_service.is_allowed_email("attacker@hotmail.com") is False
    assert token_service.is_allowed_email("student@iisc.org") is False
    assert token_service.is_allowed_email("malicious@fake-iisc.ac.in.com") is False
    assert token_service.is_allowed_email("") is False


def test_token_generation_and_verification():
    tok = token_service.create_access_token(
        user_id="std_101",
        email="student.test@iisc.ac.in",
        name="Test Student",
        roles=["student"],
        department="cds",
    )
    assert isinstance(tok, str) and len(tok) > 20

    payload = token_service.verify_access_token(tok)
    assert payload is not None
    assert payload["sub"] == "std_101"
    assert payload["email"] == "student.test@iisc.ac.in"
    assert payload["name"] == "Test Student"
    assert payload["roles"] == ["student"]
    assert payload["department"] == "cds"


def test_tampered_token_rejection():
    tok = token_service.create_access_token(
        user_id="std_101",
        email="student.test@iisc.ac.in",
    )
    # Tamper with the token string
    tampered = tok[:-6] + "xxxxxx"
    assert token_service.verify_access_token(tampered) is None
    assert token_service.verify_access_token("not-a-token") is None
    assert token_service.verify_access_token("") is None


def test_password_hashing_and_verification():
    raw_pass = "IISc#Thesis2024!"
    hashed = hash_password(raw_pass)

    assert hashed is not None
    assert "$" in hashed
    salt, dk = hashed.split("$")
    assert len(salt) == 32
    assert len(dk) == 64

    # Correct password verification
    assert verify_password(raw_pass, hashed) is True

    # Wrong password verification
    assert verify_password("WrongPassword123", hashed) is False
    assert verify_password("", hashed) is False
    assert verify_password(raw_pass, "corrupted_hash_format") is False


def test_user_store_registration_and_otp():
    async def run():
        store = UserStore()
        email = "ananya.sharma@csa.iisc.ac.in"

        # 1. Generate 6-digit OTP for registration
        otp = await store.generate_otp(email, purpose="register", ttl_seconds=300)
        assert len(otp) == 6
        assert otp.isdigit()

        # 2. Reject incorrect OTP
        invalid_res = await store.verify_and_consume_otp(email, "000000", purpose="register")
        assert invalid_res is False

        # 3. Accept valid OTP
        valid_res = await store.verify_and_consume_otp(email, otp, purpose="register")
        assert valid_res is True

        # 4. Enforce single-use (replay prevention)
        replay_res = await store.verify_and_consume_otp(email, otp, purpose="register")
        assert replay_res is False

        # 5. Save user record
        password = "SecureResearchPass99!"
        user_record = {
            "email": email,
            "name": "Ananya Sharma",
            "department": "csa",
            "roles": ["student"],
            "password_hash": hash_password(password),
            "user_id": "std_ananya_sharma",
            "created_at": int(time.time()),
        }
        await store.save_user(user_record)

        # 6. Retrieve saved user
        retrieved = await store.get_user(email)
        assert retrieved is not None
        assert retrieved["email"] == email
        assert retrieved["name"] == "Ananya Sharma"
        assert retrieved["department"] == "csa"
        assert retrieved["roles"] == ["student"]
        assert verify_password(password, retrieved["password_hash"]) is True
        assert verify_password("WrongPass", retrieved["password_hash"]) is False

    asyncio.run(run())


def test_user_store_password_reset():
    async def run():
        store = UserStore()
        email = "faculty.venkatesh@ece.iisc.ac.in"

        # Check existing seeded user
        user = await store.get_user(email)
        assert user is not None
        assert verify_password("campus-pass", user["password_hash"]) is True

        # Request reset OTP
        reset_otp = await store.generate_otp(email, purpose="reset", ttl_seconds=300)
        assert len(reset_otp) == 6

        # Verify and consume reset OTP
        assert await store.verify_and_consume_otp(email, reset_otp, purpose="reset") is True

        # Update password
        new_pass = "NewEceDeanPassword2024!"
        user["password_hash"] = hash_password(new_pass)
        await store.save_user(user)

        # Verify old password is now invalid and new password is valid
        updated_user = await store.get_user(email)
        assert verify_password("campus-pass", updated_user["password_hash"]) is False
        assert verify_password(new_pass, updated_user["password_hash"]) is True

    asyncio.run(run())


def test_pre_seeded_demo_accounts():
    async def run():
        store = UserStore()

        # Student demo account
        std = await store.get_user("student@iisc.ac.in")
        assert std is not None
        assert std["roles"] == ["student"]
        assert verify_password("campus-pass", std["password_hash"]) is True

        # Faculty demo account
        fac = await store.get_user("faculty.venkatesh@ece.iisc.ac.in")
        assert fac is not None
        assert "faculty" in fac["roles"]
        assert verify_password("campus-pass", fac["password_hash"]) is True

        # Admin demo account
        adm = await store.get_user("admin@serc.iisc.ac.in")
        assert adm is not None
        assert "cluster_admin" in adm["roles"]
        assert verify_password("campus-pass", adm["password_hash"]) is True

    asyncio.run(run())


def test_email_service_dispatch_and_formatting():
    service = EmailService()
    to_email = "researcher.sharma@cds.iisc.ac.in"
    otp = "654321"

    msg = service.format_otp_email(to_email, otp, purpose="register")
    assert msg["To"] == to_email
    assert "IISc Thesis RAG Gateway" in msg["From"]
    assert "654321" in msg["Subject"]

    # Test delivery dispatch and outbox audit tracking
    record = service.send_otp_sync(to_email, otp, purpose="register")
    assert record["to"] == to_email
    assert record["purpose"] == "register"
    assert record["status"] in ("sent", "failed_smtp", "queued")
    assert len(service.outbox) > 0


def test_static_html_file_content():
    static_file = Path(__file__).resolve().parents[1] / "gateway" / "static" / "index.html"
    assert static_file.exists(), "index.html must exist in gateway/static"
    content = static_file.read_text(encoding="utf-8")

    # Core titles and layout
    assert "IISc Institutional Thesis RAG" in content
    assert "Target Scope:" in content
    assert "L0 Exact Cache" in content
    assert "L1 Semantic Cache" in content

    # Method 2 Auth Modal UI
    assert "Institutional Access Gateway" in content
    assert "tabSignIn" in content
    assert "tabRegister" in content
    assert "Register with OTP" in content
    assert "regEmail" in content
    assert "sendOtpBtn" in content
    assert "regOtp" in content
    assert "regDept" in content
    assert "regRole" in content
    assert "regPassword" in content
    assert "regConfirmPassword" in content
    assert "submitRegistration" in content
    assert "requestRegisterOtp" in content

    # Security: OTP must NOT be exposed on the frontend page
    assert "dev_otp" not in content, "OTP must not be displayed on the register page"
    assert "Auto-fill Code" not in content, "Auto-fill OTP button must not appear on page"

    # Password Reset UI
    assert "viewReset" in content
    assert "resetEmail" in content
    assert "resetOtp" in content
    assert "resetNewPassword" in content
    assert "submitPasswordReset" in content

    # Demo personas
    assert "student@iisc" in content
    assert "faculty@ece" in content
    assert "admin@serc" in content


if __name__ == "__main__":
    test_token_service_domain_enforcement()
    test_token_generation_and_verification()
    test_tampered_token_rejection()
    test_password_hashing_and_verification()
    test_user_store_registration_and_otp()
    test_user_store_password_reset()
    test_pre_seeded_demo_accounts()
    test_email_service_dispatch_and_formatting()
    test_static_html_file_content()
    print("All Auth & Frontend tests (Domain, JWT, OTP, Store, Email, UI) passed successfully!")
